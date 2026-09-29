// agentcap — kernel-side activity meter for AI-agent process trees.
//
// The kernel side is policy-free: it knows nothing about which agents
// exist. Userspace (the collector shared worker) fills the `prefixes`
// LPM trie with comm prefixes at startup, so the agent set is defined
// entirely in JavaScript and matching is a single loop-free trie lookup —
// MAX_PREFIXES only sizes the map.
//
// A task is "tracked" when its comm starts with one of the prefixes (a
// word boundary must follow, so "pi" catches "pi" but not "pipewire")
// or when it is forked by a tracked task — a whole tree is covered from
// its root (gateway/CLI) down to the tools it execs, even when the root
// started long before this probe attached (it is adopted on its first
// context switch, fork, or socket/VFS call). Each tree keeps the identity
// of the prefix that rooted it — a tool exec'd by openclaw stays
// agent=openclaw — so every counter is per-agent.
//
// Attach types: tracepoints, LSM hooks and fexit trampolines only — no
// kprobes. LSM (stable security API; needs CONFIG_BPF_LSM + `bpf` in the
// boot lsm= list) covers the before-the-fact events whose semantics it
// carries exactly (connect, sendmsg); fexit supplies the results LSM
// never sees (actual received / file I/O bytes).
//
// Egress is split by rate: lifecycle events (fork/exec/exit) are rare and
// carry names, so they stream over a ring buffer; everything high-rate
// (CPU time, socket bytes, file I/O) is summed in the `counters` hash
// that userspace polls — one map walk per scrape window, not one wakeup
// per packet.
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_endian.h>

#define AF_UNIX 1
#define AF_INET 2
#define AF_INET6 10

#define S_IFMT 0170000
#define S_IFREG 0100000
#define FMODE_WRITE 0x2

#define PATH_MAX_LEN 256

#define TASK_COMM_LEN 16
#define MAX_PREFIXES 256

char LICENSE[] SEC("license") = "Dual BSD/GPL";

// Agent comm prefixes as an LPM trie: matching is ONE lookup, loop-free
// and verifier-cheap, for any number of agents. Userspace inserts each
// prefix four times — "name\0", "name-", "name_", "name." — so the
// word-boundary rule ("pi" catches "pi" and "pi-agent", never "pipewire")
// is encoded in the data, not in kernel branches. Values are the agent
// index the collector labels metrics with.
struct lpm_key {
	__u32 prefixlen; // in BITS, per LPM-trie convention
	char data[TASK_COMM_LEN]; // named `data`: the JS side binds by field name
};

struct {
	__uint(type, BPF_MAP_TYPE_LPM_TRIE);
	__uint(max_entries, MAX_PREFIXES * 4);
	__uint(map_flags, BPF_F_NO_PREALLOC); // LPM tries require it
	__type(key, struct lpm_key);
	__type(value, __u32);
} prefixes SEC(".maps");

// Lifecycle event kinds.
#define EV_FORK 1
#define EV_EXEC 2
#define EV_EXIT 3

struct agent_event {
	__u32 kind;
	__u32 agent; // index into `prefixes`
	__u32 pid;
	__u32 ppid;
	char comm[TASK_COMM_LEN];
};

// One inet connect: who dialed which destination port (addresses stay in
// the kernel; ports + DNS names are the audit-friendly egress).
struct conn_event {
	__u32 agent;
	__u32 pid;
	__u32 family;
	__u32 dport;
};

// A name-lookup payload sample. The kernel only filters and copies —
// parsing DNS label format (enc=1, dport-53 sends) or the nss-resolved
// varlink JSON (enc=2, unix-socket sends starting with '{') happens in
// userspace, where loops cost nothing. Stateful in-kernel parsing blew
// the verifier's 1M-insn budget. DoH/DoT stay invisible either way —
// that limitation is documented, not hidden.
#define DNS_ENC_LABELS 1
#define DNS_ENC_JSON 2
#define DNS_PAYLOAD 160

struct dns_event {
	__u32 agent;
	__u32 pid;
	__u32 enc;
	__u8 payload[DNS_PAYLOAD];
};

// One regular-file open, path resolved in-kernel with bpf_d_path. `write`
// is 1 when opened with write access — the security-interesting case.
struct file_event {
	__u32 agent;
	__u32 pid;
	__u32 write;
	char path[PATH_MAX_LEN];
};

// Force BTF emission so the daemon resolves the btf_struct names.
struct agent_event *_unused_event __attribute__((unused));
struct conn_event *_unused_conn __attribute__((unused));
struct dns_event *_unused_dns __attribute__((unused));
struct file_event *_unused_file __attribute__((unused));

// Counter slots. Keep in sync with SLOTS in src/collector.js.
enum {
	C_FORKS,
	C_EXECS,
	C_EXITS,
	C_NET_CONNECTS,
	C_NET_TX_BYTES,
	C_NET_RX_BYTES,
	C_VFS_RD_BYTES,
	C_VFS_WR_BYTES,
	C_CPU_NS,
	C_RUNNING, // gauge: tracked tasks alive (inc on track, dec on exit)
	C_EV_DROPS,
	C_MAX,
};

struct counter_key {
	__u32 agent;
	__u32 slot;
};

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 256 * 1024);
} events SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 64 * 1024);
} conn_events SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 128 * 1024);
} dns_events SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 256 * 1024);
} file_events SEC(".maps");

// Per-(agent, slot) u64 sums. A hash, not an array, so the agent space
// needs no compile-time size — series exist only once touched.
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, MAX_PREFIXES * C_MAX);
	__type(key, struct counter_key);
	__type(value, __u64);
} counters SEC(".maps");

// Per-task state: which agent tree it belongs to, and the ktime it was
// last scheduled in (0 = off-CPU), which is all CPU accounting needs.
struct task_state {
	__u32 agent;
	__u32 pad;
	__u64 oncpu_ns;
};

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 16384);
	__type(key, __u32);
	__type(value, struct task_state);
} tracked SEC(".maps");

static __always_inline void bump(__u32 agent, __u32 slot, __u64 n)
{
	struct counter_key k = { .agent = agent, .slot = slot };
	__u64 *v = bpf_map_lookup_elem(&counters, &k);
	if (!v) {
		__u64 zero = 0;
		bpf_map_update_elem(&counters, &k, &zero, BPF_NOEXIST);
		v = bpf_map_lookup_elem(&counters, &k);
		if (!v)
			return;
	}
	__sync_fetch_and_add(v, n);
}

// Match `comm` against the prefix trie; returns the agent index or -1.
// Runs only on tracked-map misses, so the cost is not paid per event once
// a task is adopted.
static __always_inline int agent_match(const char *comm)
{
	struct lpm_key k;
	k.prefixlen = TASK_COMM_LEN * 8;
	__builtin_memcpy(k.data, comm, TASK_COMM_LEN);
	__u32 *a = bpf_map_lookup_elem(&prefixes, &k);
	return a ? (int)*a : -1;
}

// Insert-if-absent; counts the gauge only on a real insert.
static __always_inline void track(__u32 pid, __u32 agent)
{
	struct task_state st = { .agent = agent };
	if (!bpf_map_update_elem(&tracked, &pid, &st, BPF_NOEXIST))
		bump(agent, C_RUNNING, 1);
}

// Tracked state for the current task in hook context: hash hit first,
// comm-prefix match as the self-healing fallback (adopts a matching task
// that predates the probe).
static __always_inline struct task_state *cur_state(void)
{
	__u32 pid = (__u32)bpf_get_current_pid_tgid();
	struct task_state *st = bpf_map_lookup_elem(&tracked, &pid);
	if (st)
		return st;
	char comm[TASK_COMM_LEN] = {};
	bpf_get_current_comm(&comm, sizeof(comm));
	int a = agent_match(comm);
	if (a < 0)
		return NULL;
	track(pid, a);
	return bpf_map_lookup_elem(&tracked, &pid);
}

static __always_inline void emit(__u32 kind, __u32 agent, __u32 pid,
				 __u32 ppid, const char *comm)
{
	struct agent_event *e = bpf_ringbuf_reserve(&events, sizeof(*e), 0);
	if (!e) {
		bump(agent, C_EV_DROPS, 1);
		return;
	}
	e->kind = kind;
	e->agent = agent;
	e->pid = pid;
	e->ppid = ppid;
	bpf_probe_read_kernel_str(&e->comm, sizeof(e->comm), comm);
	bpf_ringbuf_submit(e, 0);
}

// Note: on recent kernels the fork/exit tracepoints carry comm as a
// __data_loc dynamic field, while older kernels inline a char[16] —
// reading it portably is a CO-RE headache. At fork and exit `current` IS
// the parent / the dying task, so bpf_get_current_comm() sidesteps the
// layout entirely (a fork child inherits the parent comm).
SEC("tracepoint/sched/sched_process_fork")
int on_fork(struct trace_event_raw_sched_process_fork *ctx)
{
	struct task_state *st = cur_state(); // current == parent
	if (!st)
		return 0;
	__u32 agent = st->agent;
	track(ctx->child_pid, agent);
	bump(agent, C_FORKS, 1);
	char comm[TASK_COMM_LEN] = {};
	bpf_get_current_comm(&comm, sizeof(comm));
	emit(EV_FORK, agent, ctx->child_pid, ctx->parent_pid, comm);
	return 0;
}

SEC("tracepoint/sched/sched_process_exec")
int on_exec(struct trace_event_raw_sched_process_exec *ctx)
{
	// At this tracepoint current->comm is already the new binary name.
	struct task_state *st = cur_state();
	if (!st)
		return 0;
	char comm[TASK_COMM_LEN] = {};
	bpf_get_current_comm(&comm, sizeof(comm));
	bump(st->agent, C_EXECS, 1);
	emit(EV_EXEC, st->agent, ctx->pid, ctx->old_pid, comm);
	return 0;
}

SEC("tracepoint/sched/sched_process_exit")
int on_exit(struct trace_event_raw_sched_process_template *ctx)
{
	__u32 pid = ctx->pid;
	struct task_state *st = bpf_map_lookup_elem(&tracked, &pid);
	if (!st)
		return 0;
	__u32 agent = st->agent;
	bpf_map_delete_elem(&tracked, &pid);
	bump(agent, C_RUNNING, (__u64)-1);
	bump(agent, C_EXITS, 1);
	char comm[TASK_COMM_LEN] = {};
	bpf_get_current_comm(&comm, sizeof(comm));
	emit(EV_EXIT, agent, pid, 0, comm);
	return 0;
}

SEC("tracepoint/sched/sched_switch")
int on_switch(struct trace_event_raw_sched_switch *ctx)
{
	__u64 now = bpf_ktime_get_ns();

	// Incoming task: stamp its on-CPU start. The comm fallback adopts a
	// matching task the fork/exec hooks never saw (e.g. a gateway that
	// was already running when the probe attached).
	__u32 next = ctx->next_pid;
	struct task_state *st = bpf_map_lookup_elem(&tracked, &next);
	if (!st && next) {
		int a = agent_match(ctx->next_comm);
		if (a >= 0) {
			track(next, a);
			st = bpf_map_lookup_elem(&tracked, &next);
		}
	}
	if (st)
		st->oncpu_ns = now;

	// Outgoing task: bank its slice.
	__u32 prev = ctx->prev_pid;
	st = bpf_map_lookup_elem(&tracked, &prev);
	if (st && st->oncpu_ns) {
		bump(st->agent, C_CPU_NS, now - st->oncpu_ns);
		st->oncpu_ns = 0;
	}
	return 0;
}

// Only meter internet-family sockets; agents chatter a lot over unix
// sockets and pipes, and that is file I/O, not network activity.
static __always_inline bool inet_sock(struct socket *sock)
{
	__u16 family = BPF_CORE_READ(sock, sk, __sk_common.skc_family);
	return family == AF_INET || family == AF_INET6;
}

SEC("lsm/socket_connect")
int BPF_PROG(on_socket_connect, struct socket *sock,
	     struct sockaddr *address, int addrlen)
{
	__u16 family = BPF_CORE_READ(address, sa_family);
	if (family != AF_INET && family != AF_INET6)
		return 0;
	struct task_state *st = cur_state();
	if (!st)
		return 0;
	bump(st->agent, C_NET_CONNECTS, 1);

	__u16 dport = 0;
	if (family == AF_INET)
		dport = BPF_CORE_READ((struct sockaddr_in *)address, sin_port);
	else
		dport = BPF_CORE_READ((struct sockaddr_in6 *)address, sin6_port);

	struct conn_event *e = bpf_ringbuf_reserve(&conn_events, sizeof(*e), 0);
	if (!e) {
		bump(st->agent, C_EV_DROPS, 1);
		return 0;
	}
	e->agent = st->agent;
	e->pid = (__u32)bpf_get_current_pid_tgid();
	e->family = family;
	e->dport = bpf_ntohs(dport);
	bpf_ringbuf_submit(e, 0);
	return 0;
}

// The iovec field of iov_iter was renamed (iov -> __iov) around 6.4, and
// single-buffer sends became ITER_UBUF in 6.0 — CO-RE flavors cover both.
struct iov_iter___old {
	const struct iovec *iov;
} __attribute__((preserve_access_index));

// First byte pointer of the message being sent, or NULL.
static __always_inline const void *msg_base(struct msghdr *msg)
{
	__u8 t = BPF_CORE_READ(msg, msg_iter.iter_type);
	if (bpf_core_field_exists(msg->msg_iter.ubuf) &&
	    t == bpf_core_enum_value(enum iter_type, ITER_UBUF))
		return BPF_CORE_READ(msg, msg_iter.ubuf);
	if (t != bpf_core_enum_value(enum iter_type, ITER_IOVEC))
		return NULL;
	if (bpf_core_field_exists(msg->msg_iter.__iov))
		return BPF_CORE_READ(msg, msg_iter.__iov, iov_base);
	return BPF_CORE_READ((struct iov_iter___old *)&msg->msg_iter, iov,
			     iov_base);
}

// Reserve, zero, copy up to DNS_PAYLOAD bytes of the send buffer, submit.
static __always_inline void emit_dns(struct task_state *st, __u32 enc,
				     const void *base, int size,
				     __u8 first_byte_must_be)
{
	struct dns_event *e = bpf_ringbuf_reserve(&dns_events, sizeof(*e), 0);
	if (!e) {
		bump(st->agent, C_EV_DROPS, 1);
		return;
	}
	e->agent = st->agent;
	e->pid = (__u32)bpf_get_current_pid_tgid();
	e->enc = enc;
	__builtin_memset(e->payload, 0, sizeof(e->payload));
	__u32 n = size < (int)sizeof(e->payload) ? (__u32)size
						 : sizeof(e->payload);
	if (bpf_probe_read_user(e->payload, n, base) ||
	    (first_byte_must_be && e->payload[0] != first_byte_must_be) ||
	    (enc == DNS_ENC_LABELS && (e->payload[2] & 0x80))) {
		bpf_ringbuf_discard(e, 0);
		return;
	}
	bpf_ringbuf_submit(e, 0);
}

// A send to dport 53: ship the packet head; userspace parses the qname.
static __always_inline void sniff_dns(struct task_state *st,
				      struct socket *sock, struct msghdr *msg,
				      int size)
{
	__u16 dport = 0;
	struct sockaddr_in *name = (struct sockaddr_in *)BPF_CORE_READ(msg, msg_name);
	if (name) // sendto()-style: kernel copy of the destination
		dport = BPF_CORE_READ(name, sin_port);
	else // connected socket
		dport = BPF_CORE_READ(sock, sk, __sk_common.skc_dport);
	if (bpf_ntohs(dport) != 53 || size < 17)
		return;
	const void *base = msg_base(msg);
	if (base)
		emit_dns(st, DNS_ENC_LABELS, base, size, 0);
}

// glibc on systemd hosts resolves via nss-resolved — a varlink JSON call
// ({"method":"io.systemd.Resolve.ResolveHostname","parameters":{"name":…})
// over a unix socket — so port-53 never sees those lookups. Scan tracked
// tasks' unix-socket sends for the varlink marker and lift the name; the
// port-53 sniffer covers direct resolvers (c-ares, aiodns, dig).
static __always_inline void sniff_varlink(struct task_state *st,
					  struct msghdr *msg, int size)
{
	if (size < 40)
		return;
	const void *base = msg_base(msg);
	if (base) // '{' filter: skip non-JSON unix traffic in-kernel
		emit_dns(st, DNS_ENC_JSON, base, size, '{');
}

SEC("lsm/socket_sendmsg")
int BPF_PROG(on_socket_sendmsg, struct socket *sock, struct msghdr *msg,
	     int size)
{
	if (size <= 0)
		return 0;
	struct task_state *st = cur_state();
	if (!st)
		return 0;
	if (inet_sock(sock)) {
		bump(st->agent, C_NET_TX_BYTES, (__u64)size);
		sniff_dns(st, sock, msg, size);
	} else if (BPF_CORE_READ(sock, sk, __sk_common.skc_family) == AF_UNIX) {
		sniff_varlink(st, msg, size);
	}
	return 0;
}

SEC("fexit/sock_recvmsg")
int BPF_PROG(on_sock_recvmsg, struct socket *sock, struct msghdr *msg,
	     int flags, int ret)
{
	if (ret <= 0 || !inet_sock(sock))
		return 0;
	struct task_state *st = cur_state();
	if (st)
		bump(st->agent, C_NET_RX_BYTES, (__u64)ret);
	return 0;
}

SEC("fexit/vfs_read")
int BPF_PROG(on_vfs_read, struct file *file, char *buf, size_t count,
	     loff_t *pos, ssize_t ret)
{
	if (ret <= 0)
		return 0;
	struct task_state *st = cur_state();
	if (st)
		bump(st->agent, C_VFS_RD_BYTES, (__u64)ret);
	return 0;
}

SEC("fexit/vfs_write")
int BPF_PROG(on_vfs_write, struct file *file, const char *buf, size_t count,
	     loff_t *pos, ssize_t ret)
{
	if (ret <= 0)
		return 0;
	struct task_state *st = cur_state();
	if (st)
		bump(st->agent, C_VFS_WR_BYTES, (__u64)ret);
	return 0;
}

// Which files an agent opens. lsm/file_open is on the bpf_d_path allowlist,
// so the path is resolved in-kernel — no kprobe, no manual dentry walk.
// Only regular files (agents open a storm of /proc, /sys and dirs); the
// noisy shared-library reads are filtered by prefix in userspace.
SEC("lsm/file_open")
int BPF_PROG(on_file_open, struct file *file)
{
	umode_t mode = BPF_CORE_READ(file, f_inode, i_mode);
	if ((mode & S_IFMT) != S_IFREG)
		return 0;
	struct task_state *st = cur_state();
	if (!st)
		return 0;

	struct file_event *e = bpf_ringbuf_reserve(&file_events, sizeof(*e), 0);
	if (!e) {
		bump(st->agent, C_EV_DROPS, 1);
		return 0;
	}
	e->agent = st->agent;
	e->pid = (__u32)bpf_get_current_pid_tgid();
	e->write = (BPF_CORE_READ(file, f_mode) & FMODE_WRITE) ? 1 : 0;
	e->path[0] = '\0';
	bpf_d_path(&file->f_path, e->path, sizeof(e->path));
	bpf_ringbuf_submit(e, 0);
	return 0;
}

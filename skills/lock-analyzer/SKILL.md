---
name: lock-analyzer
description: Analyze Linux kernel locks (spinlock, mutex, semaphore) in crash tool to find lock owners and detect deadlock scenarios. Use when user mentions 'crash lock', 'crash 分析锁', '锁持有者', 'spinlock owner', 'mutex owner', 'semaphore', '死锁分析', 'lock contention', or needs to identify which task holds a kernel lock.
---

# Crash Kernel Lock Analyzer

Analyze kernel locks in crash tool to identify lock owners and diagnose deadlock issues.

## What This Skill Does

1. **Lock Owner Identification**: Find which task/process holds a specific lock
2. **Lock Type Analysis**: Handle spinlock, mutex, and semaphore
3. **Deadlock Detection**: Identify circular lock dependencies
4. **Lock Contention Analysis**: Find tasks waiting for locks
5. **Call Chain Tracing**: Trace how the lock was acquired

## MCP Tool Dependency

This skill uses the `aicrasher` MCP Server. Before using this skill, ensure:

1. The MCP Server is registered: `claude mcp list` should show `aicrasher`
2. A crash session is active (use `create_crash_session` or `analyze_crash` first)

If MCP Server is not registered, run:
```bash
bash scripts/install.sh
```

## Lock Types Supported

| Lock Type | Kernel Structure | Owner Field | Typical Use |
|-----------|-----------------|-------------|-------------|
| spinlock | `raw_spinlock_t` | No explicit owner (depends on implementation) | Short critical sections, interrupt handlers |
| mutex | `struct mutex` | `owner` (task_struct pointer) | Longer critical sections, sleepable |
| semaphore | `struct semaphore` | No owner (counting semaphore) | Resource counting, synchronization |

## Usage

```
/lock-analyzer <lock-address> [options]
/lock-analyzer --type mutex <mutex-address>
/lock-analyzer --deadlock-check
```

Examples:
- `/lock-analyzer 0xffffffc00012345` - Analyze lock at given address
- `/lock-analyzer --type mutex 0xffffffc00012345` - Specific mutex analysis
- `/lock-analyzer --deadlock-check` - Scan for deadlock scenarios

## Workflow

### Prerequisites: Create Crash Session

Before analyzing locks, you need an active crash session:

```python
# Use MCP tool to create session
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "analyze_crash",
  arguments: {
    "vmcore_path": "/path/to/vmcore",
    "vmlinux_path": "/path/to/vmlinux",
    "cmd_log_path": "/path/to/crash_cmd_log.jsonl"
  }
)
# Returns: {"session_id": "...", "cmd_log_path": "...", "baseline": [...]}
```

Or create session separately:
```python
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "create_crash_session",
  arguments: {
    "vmcore_path": "/path/to/vmcore",
    "vmlinux_path": "/path/to/vmlinux"
  }
)
```

### Step 1: Determine Lock Type

First identify the lock type from its address using MCP tool:

```python
# Execute crash command via MCP
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "struct -o mutex <lock-address>"
  }
)
```

Check the structure type:
- If has `owner` field → mutex
- If has `raw_lock` field → spinlock
- If has `count` or `sleepers` → semaphore

### Step 2: Analyze by Lock Type

#### Mutex Analysis

Mutex has explicit owner tracking:

```python
# Get mutex owner
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "struct mutex.owner <lock-address>"
  }
)

# Get owner task details
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "struct task_struct <owner-address>"
  }
)

# Check mutex state
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "struct mutex.count,wait_list <lock-address>"
  }
)
```

Advanced mutex analysis:
```python
# Batch commands for efficiency
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_commands",
  arguments: {
    "session_id": "<session_id>",
    "commands": [
      "struct mutex <lock-address>",
      "struct task_struct.pid,comm,state <owner-address>",
      "bt <owner-pid>"
    ]
  }
)
```

#### Spinlock Analysis

Spinlock analysis is more complex as it doesn't explicitly track owner:

```python
# Get spinlock state
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "struct raw_spinlock_t.raw_lock <lock-address>"
  }
)

# For ticket lock, check head/tail
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "struct arch_spinlock_t.tickets <lock-address>"
  }
)

# Find potential owner via stack traces
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "bt -a"
  }
)
# Then grep output for "spin_lock"
```

#### Semaphore Analysis

Semaphore is a counting semaphore:

```python
# Get semaphore count and sleepers
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "struct semaphore.count,sleepers <lock-address>"
  }
)

# Get wait list
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "struct semaphore.wait <lock-address>"
  }
)
```

### Step 3: Trace Lock Acquisition

Trace how the lock was acquired:

```python
# Get stack trace of lock holder
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "bt <owner-pid>"
  }
)

# Get stack with line numbers
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "bt -l <owner-pid>"
  }
)
```

### Step 4: Check for Deadlocks

Identify potential deadlock scenarios:

```python
# Check blocked tasks
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "ps -u"
  }
)

# Check all stack traces for mutex patterns
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "foreach bt"
  }
)

# Check priority inheritance chain
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "struct task_struct.pi_lockers <task-address>"
  }
)
```

### Step 5: Generate Report

Create comprehensive analysis report and close session:

```python
# Export command log for report
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "export_command_log",
  arguments: {
    "session_id": "<session_id>",
    "output_path": "/path/to/lock_cmd_log.jsonl"
  }
)

# Close session
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "close_crash_session",
  arguments: {
    "session_id": "<session_id>"
  }
)
```

## Quick Commands Reference

### Mutex Commands
```bash
# Quick mutex owner check
struct mutex.owner,count <addr>

# Full mutex info
struct mutex <addr>

# Find mutex in task's held locks
struct task_struct.held_locks <task_addr>
```

### Spinlock Commands
```bash
# Spinlock state
struct raw_spinlock_t <addr>

# Check ticket lock
struct arch_spinlock_t.tickets <addr>

# Find spinning tasks
foreach bt | grep spin_lock
```

### Semaphore Commands
```bash
# Semaphore count
struct semaphore.count,sleepers <addr>

# Wait list
struct semaphore.wait <addr>
```

### Deadlock Detection Commands
```bash
# Check all blocked tasks
ps -u | head -20

# Find lock dependencies
struct task_struct.blocked_on <task_addr>

# Check priority inheritance (mutex)
struct task_struct.pi_top_task <task_addr>
```

## Helper Scripts

The skill includes helper scripts for common operations (located in `scripts/`):

### analyze_mutex.sh
```bash
# Usage: ./analyze_mutex.sh <mutex-address>
# Outputs: owner PID, comm, state, waiters
```

### find_lock_owner.sh
```bash
# Usage: ./find_lock_owner.sh <lock-address> <lock-type>
# Outputs: Lock owner information
```

### deadlock_scan.sh
```bash
# Usage: ./deadlock_scan.sh
# Scans all tasks for potential deadlock chains
```

Note: These scripts are for reference. Prefer using MCP tools for direct crash integration.

## Common Use Cases

### 1. Find Who Holds a Mutex
```
User: "分析地址 0xffffffc00012345 的 mutex 持有者"

Steps via MCP:
1. run_crash_command: struct mutex.owner 0xffffffc00012345
2. run_crash_command: struct task_struct <owner_addr>
3. run_crash_command: bt <pid>
```

### 2. Debug Deadlock Scenario
```
User: "系统死锁了，帮我分析"

Steps via MCP:
1. run_crash_command: ps -u  # Find blocked tasks
2. run_crash_command: bt -a  # Get all stack traces
3. Analyze lock chains
4. Find circular dependency
```

### 3. Check Spinlock Contention
```
User: "CPU占用高，可能是spinlock contention"

Steps via MCP:
1. run_crash_command: foreach bt | grep spin_lock
2. Identify hot spinlocks
3. Analyze lock holders
```

## Kernel Version Differences

Different kernel versions have different lock implementations:

### Pre-4.8 Mutex
```bash
# Old mutex structure (no explicit owner field)
struct mutex {
    atomic_t count;
    spinlock_t wait_lock;
    struct list_head wait_list;
}
```

### Post-4.8 Mutex (with optimistic spinning)
```bash
struct mutex {
    atomic_long_t owner;
    atomic_t count;
    spinlock_t wait_lock;
    struct list_head wait_list;
    struct optimistic_spin_queue osq;
}
```

Check kernel version first:
```python
mcp_call_tool(
  serverName: "aicrasher",
  toolName: "run_crash_command",
  arguments: {
    "session_id": "<session_id>",
    "command": "sys"
  }
)
```

## Integration with Other Skills

- Use `/vmcore-analyzer` skill for complete vmcore analysis workflow
- Use `/kernel-build` skill for kernel compilation
- Use `/qemu-test` to test kernel with specific lock patches

## Output Requirements

Save analysis results to:
```
lock_analysis/
├── owner_info.txt      # Lock owner details
├── waiters.txt         # Tasks waiting for lock
├── stack_traces.txt    # Stack traces of owner/waiters
├── deadlock_chain.txt  # If deadlock detected
└── summary.md          # Analysis summary report
```

## Tips and Best Practices

1. **Always check kernel version first** - Lock structures vary by version
2. **Use MCP tools for crash commands** - Ensures proper session management
3. **Check multiple CPUs** - Spinlock holders might be on different CPU
4. **Look at timestamps** - Long-held locks may indicate problems
5. **Cross-reference with logs** - Match crash analysis with kernel logs
6. **Close session after analysis** - Use `close_crash_session` to cleanup
#!/bin/bash
# analyze_mutex.sh - Analyze mutex lock in crash tool
# Usage: ./analyze_mutex.sh <mutex-address> [crash-session]

MUTEX_ADDR="$1"
CRASH_SESSION="$2"

if [ -z "$MUTEX_ADDR" ]; then
    echo "Usage: $0 <mutex-address> [crash-session-file]"
    echo "Example: $0 0xffffffc00012345"
    exit 1
fi

echo "========================================"
echo "Mutex Analysis for $MUTEX_ADDR"
echo "========================================"

# Create crash command file
CRASH_CMDS=$(mktemp)
OUTPUT_DIR="lock_analysis_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_DIR"

cat > "$CRASH_CMDS" << 'EOF'
echo "=== Mutex Structure ==="
struct mutex {ARG1}

echo ""
echo "=== Lock State ==="
struct mutex.count {ARG1}
printf "Count: %d (0=locked, 1=unlocked)\n", {ARG1}->count.counter

echo ""
echo "=== Owner Information ==="
struct mutex.owner {ARG1}
if ({ARG1}->owner != 0) then
    struct task_struct.pid,comm,state {ARG1}->owner
    printf "Owner PID: %d\n", {ARG1}->owner->pid
    printf "Owner Comm: %s\n", {ARG1}->owner->comm
    printf "Owner State: %lx\n", {ARG1}->owner->state
    echo ""
    echo "=== Owner Stack Trace ==="
    bt {ARG1}->owner->pid
else
    echo "No owner (unlocked)"
fi

echo ""
echo "=== Wait List ==="
struct mutex.wait_list {ARG1}
if ({ARG1}->wait_list.next != {ARG1}->wait_list.prev) then
    echo "Tasks waiting on this mutex:"
    list task_struct.thread_node -s task_struct.pid,comm,state -H {ARG1}->wait_list
else
    echo "No waiters"
fi

echo ""
echo "=== Mutex Flags ==="
struct mutex.wait_lock {ARG1}
struct mutex.osq {ARG1}
EOF

# Replace placeholder with actual address
sed -i "s/{ARG1}/$MUTEX_ADDR/g" "$CRASH_CMDS"

echo ""
echo "Crash commands to run:"
echo "----------------------"
cat "$CRASH_CMDS"
echo ""
echo "----------------------"
echo ""
echo "Instructions:"
echo "1. Run crash tool on your vmcore: crash vmlinux vmcore"
echo "2. Copy and paste the commands above"
echo "3. Or run: crash -i $CRASH_CMDS vmlinux vmcore"
echo ""
echo "Output will be saved to: $OUTPUT_DIR/"
echo ""

# Save command file for later use
cp "$CRASH_CMDS" "$OUTPUT_DIR/crash_commands.txt"

# If crash session is provided, run automatically
if [ -n "$CRASH_SESSION" ] && [ -f "$CRASH_SESSION" ]; then
    echo "Running crash analysis..."
    crash -i "$CRASH_CMDS" "$CRASH_SESSION" > "$OUTPUT_DIR/mutex_analysis.txt"
    echo "Results saved to: $OUTPUT_DIR/mutex_analysis.txt"
fi

rm -f "$CRASH_CMDS"
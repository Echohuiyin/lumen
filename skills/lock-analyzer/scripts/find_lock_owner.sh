#!/bin/bash
# find_lock_owner.sh - Find lock owner for any lock type
# Usage: ./find_lock_owner.sh <lock-address> <lock-type>
# lock-type: mutex, spinlock, semaphore

LOCK_ADDR="$1"
LOCK_TYPE="$2"

if [ -z "$LOCK_ADDR" ]; then
    echo "Usage: $0 <lock-address> <lock-type>"
    echo "Lock types: mutex, spinlock, semaphore"
    echo "Example: $0 0xffffffc00012345 mutex"
    exit 1
fi

if [ -z "$LOCK_TYPE" ]; then
    echo "Auto-detecting lock type..."
    # Try to detect lock type from structure
    LOCK_TYPE="auto"
fi

OUTPUT_DIR="lock_analysis_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_DIR"

echo "========================================"
echo "Lock Owner Analysis"
echo "========================================"
echo "Lock Address: $LOCK_ADDR"
echo "Lock Type: $LOCK_TYPE"
echo "Output Directory: $OUTPUT_DIR"
echo ""

case "$LOCK_TYPE" in
    mutex)
        echo "=== Analyzing Mutex ==="
        CRASH_CMDS=$(mktemp)
        cat > "$CRASH_CMDS" << EOF
struct mutex $LOCK_ADDR
struct mutex.owner $LOCK_ADDR
if ($LOCK_ADDR->owner != 0) then
    printf "\n=== Lock Owner Found ===\n"
    struct task_struct.pid,comm,state,prio $LOCK_ADDR->owner
    printf "\nPID: %d\n", $LOCK_ADDR->owner->pid
    printf "Command: %s\n", $LOCK_ADDR->owner->comm
    printf "State: 0x%lx\n", $LOCK_ADDR->owner->state
    printf "Priority: %d\n", $LOCK_ADDR->owner->prio
    printf "\n=== Owner Stack Trace ===\n"
    bt $LOCK_ADDR->owner->pid
else
    printf "Mutex is unlocked (no owner)\n"
fi
printf "\n=== Lock State ===\n"
printf "Count: %d (0=locked, 1=unlocked)\n", $LOCK_ADDR->count.counter
printf "\n=== Waiters ===\n"
struct mutex.wait_list $LOCK_ADDR
if ($LOCK_ADDR->wait_list.next != $LOCK_ADDR->wait_list.prev) then
    list task_struct.thread_node -s task_struct.pid,comm,state -H $LOCK_ADDR->wait_list
else
    printf "No waiters\n"
fi
EOF
        ;;
    spinlock)
        echo "=== Analyzing Spinlock ==="
        CRASH_CMDS=$(mktemp)
        cat > "$CRASH_CMDS" << EOF
struct raw_spinlock_t $LOCK_ADDR
printf "\n=== Spinlock State ===\n"
struct arch_spinlock_t.tickets $LOCK_ADDR->raw_lock
printf "\nTicket Lock Analysis:\n"
printf "  Head (serving): %d\n", $LOCK_ADDR->raw_lock.tickets.head
printf "  Tail (next): %d\n", $LOCK_ADDR->raw_lock.tickets.tail
if ($LOCK_ADDR->raw_lock.tickets.head == $LOCK_ADDR->raw_lock.tickets.tail) then
    printf "  Status: UNLOCKED\n"
else
    printf "  Status: LOCKED\n"
    printf "  Waiters: %d\n", ($LOCK_ADDR->raw_lock.tickets.tail - $LOCK_ADDR->raw_lock.tickets.head)
fi
printf "\n=== Finding Potential Owner ===\n"
printf "Spinlocks don't track owner explicitly.\n"
printf "Search for tasks with spin_lock in stack:\n"
foreach bt | grep -A5 spin_lock
printf "\n=== Current Tasks per CPU ===\n"
struct cpu_rq
EOF
        ;;
    semaphore)
        echo "=== Analyzing Semaphore ==="
        CRASH_CMDS=$(mktemp)
        cat > "$CRASH_CMDS" << EOF
struct semaphore $LOCK_ADDR
printf "\n=== Semaphore State ===\n"
printf "Count: %d\n", $LOCK_ADDR->count
printf "Sleepers: %d\n", $LOCK_ADDR->sleepers
if ($LOCK_ADDR->count > 0) then
    printf "Status: AVAILABLE (unlocked)\n"
else
    printf "Status: UNAVAILABLE (locked)\n"
fi
printf "\n=== Wait Queue ===\n"
struct semaphore.wait $LOCK_ADDR
if ($LOCK_ADDR->wait.task_list.next != $LOCK_ADDR->wait.task_list.prev) then
    printf "Tasks waiting:\n"
    list -s task_struct.pid,comm,state -H $LOCK_ADDR->wait.task_list
else
    printf "No waiters\n"
fi
printf "\n=== Note ===\n"
printf "Semaphore does not track owner.\n"
printf "Owner is whoever last called down() successfully.\n"
EOF
        ;;
    auto)
        echo "=== Auto-detecting Lock Type ==="
        CRASH_CMDS=$(mktemp)
        cat > "$CRASH_CMDS" << EOF
printf "Trying to identify lock type at $LOCK_ADDR...\n\n"
printf "=== Check if Mutex ===\n"
struct mutex.owner $LOCK_ADDR
printf "\n=== Check if Spinlock ===\n"
struct raw_spinlock_t.raw_lock $LOCK_ADDR
printf "\n=== Check if Semaphore ===\n"
struct semaphore.count $LOCK_ADDR
EOF
        ;;
    *)
        echo "Unknown lock type: $LOCK_TYPE"
        echo "Supported: mutex, spinlock, semaphore, auto"
        exit 1
        ;;
esac

echo ""
echo "Crash commands:"
echo "---------------"
cat "$CRASH_CMDS"
echo "---------------"
echo ""
echo "Run these commands in crash tool:"
echo "  crash vmlinux vmcore"
echo "  (paste commands above)"
echo ""
echo "Or run automatically:"
echo "  crash -i $CRASH_CMDS vmlinux vmcore > $OUTPUT_DIR/analysis.txt"
echo ""

cp "$CRASH_CMDS" "$OUTPUT_DIR/crash_commands.txt"
rm -f "$CRASH_CMDS"
#!/bin/bash
# deadlock_scan.sh - Scan for potential deadlock scenarios
# Usage: ./deadlock_scan.sh

OUTPUT_DIR="deadlock_scan_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_DIR"

echo "========================================"
echo "Deadlock Detection Scan"
echo "========================================"
echo "Output: $OUTPUT_DIR"
echo ""

CRASH_CMDS=$(mktemp)

cat > "$CRASH_CMDS" << 'EOF'
echo "=== Step 1: Find Blocked Tasks ==="
ps -u
printf "\nBlocked tasks (TASK_UNINTERRUPTIBLE):\n"

echo ""
echo "=== Step 2: Get All Stack Traces ==="
foreach bt > all_stacks.txt
printf "Stack traces saved to all_stacks.txt\n"

echo ""
echo "=== Step 3: Analyze Lock Chains ==="
printf "Looking for mutex_lock in blocked tasks:\n"
ps -u | awk '{print $1}' | while read pid; do
    if [ "$pid" != "PID" ] && [ "$pid" != "" ]; then
        bt $pid | grep -A2 mutex_lock
    fi
done

echo ""
echo "=== Step 4: Check Circular Dependencies ==="
printf "Tasks blocked on locks:\n"
foreach struct task_struct.blocked_on

echo ""
echo "=== Step 5: Priority Inheritance Check ==="
printf "Tasks with PI (priority inheritance) lockers:\n"
foreach struct task_struct.pi_lockers

echo ""
echo "=== Step 6: Find Held Locks ==="
printf "Locks currently held by tasks:\n"
foreach struct task_struct.lockdep_depth
foreach struct task_struct.held_locks

echo ""
echo "=== Summary ==="
printf "\nPotential deadlock indicators:\n"
printf "1. Multiple tasks in TASK_UNINTERRUPTIBLE state\n"
printf "2. Circular lock dependency in stack traces\n"
printf "3. PI chain longer than 1 level\n"
printf "4. Same lock appears in multiple held_locks\n"
EOF

echo "Crash commands to execute:"
echo "-------------------------"
cat "$CRASH_CMDS"
echo "-------------------------"
echo ""
echo "Instructions:"
echo "1. Run: crash vmlinux vmcore"
echo "2. Paste the commands above"
echo "3. Review output for potential deadlocks"
echo ""
echo "Or run automatically:"
echo "  crash -i $CRASH_CMDS vmlinux vmcore | tee $OUTPUT_DIR/deadlock_analysis.txt"
echo ""

cp "$CRASH_CMDS" "$OUTPUT_DIR/crash_commands.txt"

# Create Python helper for deadlock visualization
cat > "$OUTPUT_DIR/deadlock_visualizer.py" << 'PYEOF'
#!/usr/bin/env python3
"""
Deadlock chain visualizer
Parse crash output and create deadlock dependency graph
"""

import sys
import re
from collections import defaultdict

def parse_blocked_tasks(crash_output):
    """Parse ps -u output to find blocked tasks"""
    blocked = []
    for line in crash_output.split('\n'):
        if 'UN' in line:  # TASK_UNINTERRUPTIBLE
            parts = line.split()
            if len(parts) >= 2:
                blocked.append({
                    'pid': parts[0],
                    'task_addr': parts[1],
                    'comm': parts[-1] if len(parts) > 2 else 'unknown'
                })
    return blocked

def parse_lock_chain(bt_output):
    """Parse bt output to find lock acquisition chain"""
    chain = []
    mutex_pattern = re.compile(r'mutex_lock.*at\s+(\S+)')
    for line in bt_output.split('\n'):
        match = mutex_pattern.search(line)
        if match:
            chain.append(match.group(1))
    return chain

def build_dependency_graph(tasks_data):
    """Build lock dependency graph"""
    graph = defaultdict(list)
    for task in tasks_data:
        # task A -> holds lock L1 -> waiting for lock L2 <- task B
        # This creates dependency: A -> B
        if task['held_lock'] and task['waiting_for']:
            graph[task['pid']].append(task['waiting_for_task'])
    return graph

def detect_cycle(graph):
    """Detect circular dependency (deadlock)"""
    visited = set()
    rec_stack = set()
    cycle = []

    def dfs(node, path):
        visited.add(node)
        rec_stack.add(node)
        path.append(node)

        for neighbor in graph[node]:
            if neighbor not in visited:
                result = dfs(neighbor, path)
                if result:
                    return result
            elif neighbor in rec_stack:
                # Found cycle
                cycle_start = path.index(neighbor)
                return path[cycle_start:] + [neighbor]

        path.pop()
        rec_stack.remove(node)
        return None

    for node in graph:
        if node not in visited:
            result = dfs(node, [])
            if result:
                return result

    return None

def print_deadlock_report(cycle):
    """Generate deadlock report"""
    print("=" * 50)
    print("DEADLOCK DETECTED!")
    print("=" * 50)
    print("\nCircular lock dependency chain:")
    for i, pid in enumerate(cycle):
        print(f"  [{i}] PID {pid} ->")
    print(f"  Back to PID {cycle[0]}")
    print("\nThis forms a deadlock cycle!")
    print("\nRecommendation:")
    print("  1. Check lock ordering in code")
    print("  2. Review stack traces for each task")
    print("  3. Identify common lock causing cycle")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python deadlock_visualizer.py <crash_output_file>")
        sys.exit(1)

    with open(sys.argv[1], 'r') as f:
        crash_output = f.read()

    blocked = parse_blocked_tasks(crash_output)
    print(f"Found {len(blocked)} blocked tasks")

    # Build dependency graph from parsed data
    graph = build_dependency_graph(blocked)

    # Check for deadlock cycle
    cycle = detect_cycle(graph)
    if cycle:
        print_deadlock_report(cycle)
    else:
        print("No obvious deadlock cycle detected")
        print("Manual analysis recommended")
PYEOF

chmod +x "$OUTPUT_DIR/deadlock_visualizer.py"

rm -f "$CRASH_CMDS"

echo "Helper scripts created:"
echo "  - $OUTPUT_DIR/crash_commands.txt"
echo "  - $OUTPUT_DIR/deadlock_visualizer.py"
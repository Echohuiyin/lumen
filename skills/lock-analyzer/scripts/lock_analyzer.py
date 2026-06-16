#!/usr/bin/env python3
"""
Lock Analyzer for Crash Tool Output
Parse crash command output and generate comprehensive lock analysis report

Usage:
    python lock_analyzer.py --input crash_output.txt --output report.md
    python lock_analyzer.py --mutex <mutex-addr> --crash <vmcore-path>
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

class LockAnalyzer:
    """Analyze kernel locks from crash tool output"""

    LOCK_TYPES = {
        'mutex': {
            'owner_field': 'owner',
            'state_field': 'count',
            'wait_field': 'wait_list',
            'locked_value': 0,
        },
        'spinlock': {
            'owner_field': None,  # No explicit owner
            'state_field': 'tickets',
            'wait_field': None,
            'locked_value': 'tail != head',
        },
        'semaphore': {
            'owner_field': None,  # No owner (counting)
            'state_field': 'count',
            'wait_field': 'wait',
            'locked_value': 0,
        }
    }

    def __init__(self):
        self.analysis_result = {}

    def parse_mutex_struct(self, output):
        """Parse struct mutex output from crash"""
        result = {
            'address': None,
            'owner': None,
            'count': None,
            'waiters': [],
            'locked': False,
        }

        # Extract address
        addr_match = re.search(r'struct mutex @ (0x[0-9a-f]+)', output)
        if addr_match:
            result['address'] = addr_match.group(1)

        # Extract count
        count_match = re.search(r'count\.counter\s*=\s*(\d+)', output)
        if count_match:
            result['count'] = int(count_match.group(1))
            result['locked'] = result['count'] == 0

        # Extract owner
        owner_match = re.search(r'owner\s*=\s*(0x[0-9a-f]+)', output)
        if owner_match and owner_match.group(1) != '0x0':
            result['owner'] = owner_match.group(1)

        return result

    def parse_task_struct(self, output):
        """Parse struct task_struct output"""
        result = {
            'address': None,
            'pid': None,
            'comm': None,
            'state': None,
        }

        pid_match = re.search(r'pid\s*=\s*(\d+)', output)
        if pid_match:
            result['pid'] = int(pid_match.group(1))

        comm_match = re.search(r'comm\s*=\s*"([^"]+)"', output)
        if comm_match:
            result['comm'] = comm_match.group(1)

        state_match = re.search(r'state\s*=\s*(0x[0-9a-f]+|\d+)', output)
        if state_match:
            result['state'] = state_match.group(1)

        return result

    def parse_spinlock_struct(self, output):
        """Parse spinlock structure output"""
        result = {
            'address': None,
            'head': None,
            'tail': None,
            'locked': False,
            'waiters_count': 0,
        }

        # Extract tickets
        head_match = re.search(r'head\s*=\s*(\d+)', output)
        tail_match = re.search(r'tail\s*=\s*(\d+)', output)

        if head_match and tail_match:
            result['head'] = int(head_match.group(1))
            result['tail'] = int(tail_match.group(1))
            result['locked'] = result['tail'] != result['head']
            result['waiters_count'] = result['tail'] - result['head']

        return result

    def parse_semaphore_struct(self, output):
        """Parse semaphore structure output"""
        result = {
            'address': None,
            'count': None,
            'sleepers': None,
            'locked': False,
        }

        count_match = re.search(r'count\s*=\s*(\d+)', output)
        if count_match:
            result['count'] = int(count_match.group(1))
            result['locked'] = result['count'] == 0

        sleepers_match = re.search(r'sleepers\s*=\s*(\d+)', output)
        if sleepers_match:
            result['sleepers'] = int(sleepers_match.group(1))

        return result

    def parse_bt_output(self, output):
        """Parse bt (backtrace) output"""
        result = {
            'pid': None,
            'frames': [],
        }

        # Extract PID
        pid_match = re.search(r'PID:\s*(\d+)', output)
        if pid_match:
            result['pid'] = int(pid_match.group(1))

        # Extract frames
        frame_pattern = re.compile(r'#\d+\s+\[.*?\]\s+(.+?)\s+at\s+(.+?)(?:\s|$)')
        for match in frame_pattern.finditer(output):
            result['frames'].append({
                'function': match.group(1),
                'location': match.group(2),
            })

        return result

    def generate_report(self, lock_info, owner_info=None, stack_trace=None, waiters=None):
        """Generate comprehensive analysis report"""
        report_lines = []

        report_lines.append("# Lock Analysis Report")
        report_lines.append(f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        # Lock Information
        report_lines.append("## Lock Information")
        report_lines.append(f"- **Address**: `{lock_info.get('address', 'N/A')}`")
        report_lines.append(f"- **Type**: {lock_info.get('type', 'Unknown')}")
        report_lines.append(f"- **State**: {'LOCKED' if lock_info.get('locked') else 'UNLOCKED'}")

        if lock_info.get('type') == 'mutex':
            report_lines.append(f"- **Count**: {lock_info.get('count', 'N/A')} (0=locked, 1=unlocked)")
        elif lock_info.get('type') == 'spinlock':
            report_lines.append(f"- **Head Ticket**: {lock_info.get('head', 'N/A')}")
            report_lines.append(f"- **Tail Ticket**: {lock_info.get('tail', 'N/A')}")
            report_lines.append(f"- **Waiters**: {lock_info.get('waiters_count', 0)}")
        elif lock_info.get('type') == 'semaphore':
            report_lines.append(f"- **Count**: {lock_info.get('count', 'N/A')}")
            report_lines.append(f"- **Sleepers**: {lock_info.get('sleepers', 'N/A')}")

        # Lock Owner
        if owner_info:
            report_lines.append("\n## Lock Owner")
            report_lines.append(f"- **PID**: {owner_info.get('pid', 'N/A')}")
            report_lines.append(f"- **Command**: `{owner_info.get('comm', 'N/A')}`")
            report_lines.append(f"- **State**: `{owner_info.get('state', 'N/A')}`")

            state_decoding = {
                '0x0': 'TASK_RUNNING',
                '0x1': 'TASK_INTERRUPTIBLE',
                '0x2': 'TASK_UNINTERRUPTIBLE',
                '0x4': 'TASK_STOPPED',
                '0x8': 'TASK_TRACED',
            }
            state_val = owner_info.get('state', '')
            if state_val in state_decoding:
                report_lines.append(f"  - {state_decoding[state_val]}")
        else:
            report_lines.append("\n## Lock Owner")
            report_lines.append("- **No explicit owner** (lock may be unlocked or spinlock/semaphore)")

        # Stack Trace
        if stack_trace:
            report_lines.append("\n## Owner Stack Trace")
            report_lines.append("```")
            for i, frame in enumerate(stack_trace.get('frames', [])):
                report_lines.append(f"#{i} {frame['function']} at {frame['location']}")
            report_lines.append("```")

        # Waiters
        if waiters and len(waiters) > 0:
            report_lines.append("\n## Tasks Waiting for Lock")
            for w in waiters:
                report_lines.append(f"- PID {w.get('pid', 'N/A')}: `{w.get('comm', 'unknown')}` (state: `{w.get('state', 'N/A')}`)")

        # Analysis Summary
        report_lines.append("\n## Analysis Summary")
        if lock_info.get('locked'):
            if lock_info.get('type') == 'mutex' and owner_info:
                report_lines.append(f"- Lock is held by PID {owner_info.get('pid', 'N/A')}")
            elif lock_info.get('type') == 'spinlock':
                report_lines.append("- Spinlock is locked but has no explicit owner tracking")
                report_lines.append("- Potential owner may be found by analyzing stack traces")
            elif lock_info.get('type') == 'semaphore':
                report_lines.append("- Semaphore count is 0 (locked)")
                report_lines.append("- Semaphore does not track owner")
        else:
            report_lines.append("- Lock is currently unlocked")

        if waiters and len(waiters) > 0:
            report_lines.append(f"- {len(waiters)} tasks are waiting for this lock")

        # Recommendations
        report_lines.append("\n## Recommendations")
        if lock_info.get('locked') and waiters and len(waiters) > 0:
            report_lines.append("- Check if lock holder is stuck or taking too long")
            report_lines.append("- Analyze stack trace for potential deadlock")
            report_lines.append("- Consider lock timeout or debugging")

        return '\n'.join(report_lines)

    def analyze_from_crash_file(self, input_file, output_file):
        """Analyze crash output from file"""
        with open(input_file, 'r') as f:
            content = f.read()

        # Try to parse different sections
        lock_info = {}

        # Detect mutex output
        if 'struct mutex' in content:
            lock_info = self.parse_mutex_struct(content)
            lock_info['type'] = 'mutex'

        # Detect spinlock output
        elif 'raw_spinlock_t' in content or 'tickets' in content:
            lock_info = self.parse_spinlock_struct(content)
            lock_info['type'] = 'spinlock'

        # Detect semaphore output
        elif 'struct semaphore' in content:
            lock_info = self.parse_semaphore_struct(content)
            lock_info['type'] = 'semaphore'

        # Parse owner info if present
        owner_info = None
        if 'struct task_struct' in content:
            owner_info = self.parse_task_struct(content)

        # Parse stack trace if present
        stack_trace = None
        if 'PID:' in content and '#0' in content:
            stack_trace = self.parse_bt_output(content)

        # Generate report
        report = self.generate_report(lock_info, owner_info, stack_trace)

        # Write output
        output_path = Path(output_file)
        output_path.write_text(report)

        print(f"Analysis report generated: {output_file}")
        return report


def main():
    parser = argparse.ArgumentParser(
        description='Analyze kernel locks from crash tool output'
    )
    parser.add_argument(
        '--input', '-i',
        help='Input file containing crash command output'
    )
    parser.add_argument(
        '--output', '-o',
        default='lock_analysis_report.md',
        help='Output report file (default: lock_analysis_report.md)'
    )
    parser.add_argument(
        '--mutex',
        help='Mutex address to analyze'
    )
    parser.add_argument(
        '--spinlock',
        help='Spinlock address to analyze'
    )
    parser.add_argument(
        '--semaphore',
        help='Semaphore address to analyze'
    )
    parser.add_argument(
        '--crash',
        help='Path to vmcore for crash analysis'
    )

    args = parser.parse_args()

    analyzer = LockAnalyzer()

    if args.input:
        analyzer.analyze_from_crash_file(args.input, args.output)
    else:
        # Generate crash commands
        print("No input file provided. Generating crash commands...")
        print("\nRun these commands in crash tool:")
        print("-" * 40)

        if args.mutex:
            print(f"struct mutex {args.mutex}")
            print(f"struct mutex.owner {args.mutex}")
            print(f"if ({args.mutex}->owner) struct task_struct {args.mutex}->owner")
        elif args.spinlock:
            print(f"struct raw_spinlock_t {args.spinlock}")
            print(f"struct arch_spinlock_t.tickets {args.spinlock}->raw_lock")
        elif args.semaphore:
            print(f"struct semaphore {args.semaphore}")
            print(f"struct semaphore.count {args.semaphore}")

        print("-" * 40)
        print("\nAfter running, save output and run:")
        print(f"  python {sys.argv[0]} -i <saved-output> -o report.md")


if __name__ == '__main__':
    main()
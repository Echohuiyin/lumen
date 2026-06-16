---
name: kernel-testcase-generator
description: Linux kernel problem reproducer constructor. Automatically generate reproducible test cases based on problem analysis results (knowledge base search, lock analysis, vmcore crash analysis, kernel log analysis). Use this skill when user needs to create test cases to reproduce kernel bugs, panic scenarios, deadlock situations, or any kernel-level issues for verification purposes.
---

# Kernel Testcase Generator

Expert skill for constructing reproducible test cases based on kernel problem analysis results.

## What This Skill Does

This skill acts as a kernel expert specializing in creating reproducible test cases. It takes analysis results as input and generates code that can reliably reproduce the identified kernel problem.

**Input Sources:**
- Knowledge base search results (historical similar issues)
- Lock analysis results (from `/lock-analyzer`)
- Vmcore crash analysis results (from `/vmcore-analyzer`)
- Kernel log analysis results (dmesg, kernel messages)

**Output:**
- Reproducible test case code (kernel module, user program, or combination)
- Brief developer self-verification (compilation + basic functionality check)

## When to Trigger

Use this skill when:
- User asks to "create a reproducer" or "construct a test case" for a kernel problem
- User mentions "复现问题", "构造用例", "test reproducer"
- User provides analysis results and wants code to reproduce the issue
- After completing `/vmcore-analyzer` or `/lock-analyzer` and wants to verify findings
- User wants to create regression tests for kernel bugs

## Workflow

### Step 0: Input Analysis

Before generating any code, thoroughly analyze all provided inputs:

1. **Knowledge Base Results**: Check if similar issues have been reproduced before
   - Look for existing reproducer patterns
   - Identify successful reproduction methods
   - Note key triggering conditions

2. **Lock Analysis Results**: If lock-related issue
   - Identify lock type (spinlock/mutex/semaphore)
   - Understand lock contention scenario
   - Determine deadlock chain if present

3. **Vmcore Crash Analysis**: Extract root cause and triggering conditions
   - Panic type (hung task, lockup, BUG_ON, NULL ptr, etc.)
   - Call stack leading to crash
   - Specific kernel subsystem involved
   - Code path and conditions that trigger the bug

4. **Kernel Logs**: Identify symptom patterns
   - Warning/error messages before crash
   - Timing information
   - User-space triggers if visible

**Key Question**: What specific conditions trigger this kernel bug?

### Step 1: Choose Reproducer Type

Based on the problem characteristics, automatically select the most appropriate reproducer type:

| Problem Type | Preferred Reproducer | Rationale |
|--------------|---------------------|-----------|
| **Race condition / deadlock** | Kernel module | Precise control over timing and locking |
| **Syscall-triggered bug** | User program | Tests from syscall entry point |
| **Filesystem/VFS bug** | User program + mount ops | Real filesystem operations trigger |
| **Memory corruption** | Kernel module | Direct memory manipulation needed |
| **Driver/hardware issue** | Kernel module + user trigger | Driver interface testing |
| **Scheduler/CPU hotplug** | Kernel module + sysfs ops | Scheduler state manipulation |
| **OOM/memory pressure** | User program (malloc stress) | User-space memory allocation |

**Decision Logic:**
```python
def choose_reproducer_type(problem_type, subsystem):
    if problem_type in ["race_condition", "deadlock", "memory_corruption"]:
        return "kernel_module"
    elif problem_type in ["syscall_bug", "filesystem_bug"]:
        return "user_program"
    elif problem_type in ["driver_bug", "scheduler_bug"]:
        return "kernel_module_user_trigger"
    elif problem_type == "oom":
        return "user_program_memory_stress"
    else:
        # Default to kernel module for maximum control
        return "kernel_module"
```

### Step 2: Design Reproducer Logic

Construct the reproduction logic based on analysis:

#### For Kernel Module Reproducers

**Template Structure:**
```c
// reproducer.c
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/...>  // Subsystem-specific headers

// Global state
static struct reproducer_state {
    // Variables to trigger the bug
};

// Trigger function - CRITICAL: Follow "Trigger Expected Bug, Avoid Side Effects" principle
static int trigger_bug(void) {
    // Implement bug triggering logic based on:
    // 1. Code path from crash analysis
    // 2. Conditions from lock analysis
    // 3. State manipulation from root cause

    // ⚠️ IMPORTANT: Only trigger the EXPECTED bug from analysis
    // ❌ DON'T introduce random bugs (e.g., uninitialized variables, wrong NULL ptr)
    // ✅ DO trigger specific bug at analyzed location (e.g., NULL ptr at analyzed line)

    return 0;  // or trigger panic/crash at expected location
}

// Module init
static int __init reproducer_init(void) {
    printk(KERN_INFO "Reproducer loaded\n");

    // Setup initial state to create conditions for expected bug
    // ⚠️ Ensure proper initialization to avoid unrelated crashes

    // Trigger the bug at analyzed location
    trigger_bug();

    return 0;
}

// Module exit
static void __exit reproducer_exit(void) {
    printk(KERN_INFO "Reproducer unloaded\n");
}

module_init(reproducer_init);
module_exit(reproducer_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Kernel Expert");
MODULE_DESCRIPTION("Reproducer for <bug description>");
```

#### For User Program Reproducers

**Template Structure:**
```c
// reproducer.c (user-space)
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/...>  // Required syscalls

int main(int argc, char *argv[]) {
    printf("Starting reproducer...\n");

    // Trigger syscall path based on:
    // 1. Syscall number/entry point from crash analysis
    // 2. Specific parameters causing the bug
    // 3. Timing/iteration patterns

    return 0;
}
```

#### For Combined Reproducers

**When kernel module creates condition + user program triggers:**

1. Kernel module: Setup vulnerable state
2. User program: Trigger syscall/ioctl to hit the vulnerable path

**Example:**
```c
// kernel module
static long reproducer_ioctl(struct file *file, unsigned int cmd, unsigned long arg) {
    // Setup state for bug
    switch (cmd) {
    case TRIGGER_CMD:
        return trigger_bug();
    }
    return 0;
}

// user program
int main() {
    int fd = open("/dev/reproducer", O_RDWR);
    ioctl(fd, TRIGGER_CMD, 0);
    close(fd);
}
```

### Step 3: Write Makefile / Build Script

Provide build instructions:

**Kernel Module Makefile:**
```makefile
obj-m += reproducer.o

KDIR := /lib/modules/$(shell uname -r)/build

all:
	make -C $(KDIR) M=$(PWD) modules

clean:
	make -C $(KDIR) M=$(PWD) clean
```

**User Program:**
```bash
gcc -o reproducer reproducer.c
```

### Step 4: Generate README with Usage Instructions

Create clear usage documentation:

```markdown
# Reproducer for <Bug Description>

## Build

```bash
make  # For kernel module
gcc reproducer.c -o reproducer  # For user program
```

## Run

```bash
sudo insmod reproducer.ko  # Load kernel module
sudo ./reproducer          # Run user program (if needed)
sudo rmmod reproducer      # Unload (if survives)
```

## Expected Result

<What should happen when bug is triggered>

## Debug Tips

- Check dmesg for kernel messages
- Check /proc/kmsg for live kernel log
- Use crash tool to analyze if panic occurs
```

### Step 5: Developer Self-Verification

Perform **minimal but essential** verification:

#### Compilation Check (Required)

```bash
# For kernel module
make
# Check: reproducer.ko exists, no build errors

# For user program
gcc reproducer.c -o reproducer
# Check: reproducer binary exists, no compile warnings
```

#### Basic Functionality Check (Required)

**Not full testing - just sanity check:**

```bash
# For kernel module
sudo insmod reproducer.ko
lsmod | grep reproducer  # Verify loaded
sudo rmmod reproducer    # Verify unload works
dmesg | tail             # Check for load/unload messages

# For user program
./reproducer
# Check: program runs without immediate crash
# Check: dmesg shows expected kernel messages
```

**DO NOT:**
- Run full regression testing
- Test multiple kernel versions
- Test different architectures
- Stress testing or iteration testing

**Verification Scope:**
- ✅ Code compiles successfully
- ✅ Module loads/unloads cleanly (for kernel module)
- ✅ Program executes basic path (for user program)
- ✅ Expected kernel messages appear in dmesg
- ❌ Full bug reproduction verification (test expert's job)

**If verification fails:**
- Fix compilation errors immediately
- Adjust code if module fails to load
- Debug basic functionality issues

## Output File Structure

Save reproducer in user-specified directory (ask if not provided):

```
<output_dir>/<bug_name>_reproducer/
├── reproducer.c        # Main reproducer code
├── Makefile            # Build script
├── README.md           # Usage instructions
├── verification.log    # Self-verification results
└── if combined reproducer:
    ├── reproducer_kmod.c  # Kernel module part
    ├── reproducer_user.c  # User program part
```

## Common Reproducer Patterns

### Pattern 1: Deadlock Reproducer

From lock analysis results:

```c
// Example: mutex deadlock reproducer
static DEFINE_MUTEX(mutex1);
static DEFINE_MUTEX(mutex2);

static int thread1_fn(void *data) {
    mutex_lock(&mutex1);
    msleep(100);
    mutex_lock(&mutex2);  // Deadlock if thread2 holds mutex2
    mutex_unlock(&mutex2);
    mutex_unlock(&mutex1);
    return 0;
}

static int thread2_fn(void *data) {
    mutex_lock(&mutex2);
    msleep(100);
    mutex_lock(&mutex1);  // Deadlock!
    mutex_unlock(&mutex1);
    mutex_unlock(&mutex2);
    return 0;
}
```

### Pattern 2: Race Condition Reproducer

From crash analysis showing race:

```c
// Example: race in shared data structure
static struct shared_data {
    int counter;
    spinlock_t lock;
};

static int race_thread(void *data) {
    // Intentionally NOT use lock to trigger race
    shared_data.counter++;
    // Or: use lock incorrectly
    spin_lock(&lock);
    shared_data.counter++;
    // Forgot to unlock - hang
    return 0;
}
```

### Pattern 3: NULL Pointer Dereference

From vmcore analysis showing NULL ptr at specific location:

```c
// Example: Trigger NULL ptr at analyzed location
// Vmcore analysis: crash in device_ioctl_handler at line X
// Root cause: file->private_data not initialized in open handler

// Kernel module implementing buggy device driver
static int buggy_open(struct inode *inode, struct file *file) {
    // ✅ INTENTIONALLY NOT setting file->private_data
    // This matches the root cause from analysis
    return 0;  // Don't initialize private_data
}

static long buggy_ioctl(struct file *file, unsigned int cmd, unsigned long arg) {
    struct my_device *dev = file->private_data;  // NULL as analyzed
    
    // ✅ Trigger crash at exact location from analysis
    // Expected crash: accessing dev->ops at this line
    return dev->ops->ioctl(dev, cmd, arg);
}

// User program triggers the buggy ioctl
int main() {
    int fd = open("/dev/buggy_device", O_RDWR);
    ioctl(fd, SOME_CMD, 0);  // Trigger crash at analyzed location
}
```

**Key difference**:
- ✅ **Trigger expected bug**: `file->private_data` is NULL because `open()` didn't initialize it (matches analysis)
- ❌ **Avoid coding error**: Don't just set `dev = NULL` randomly - that's sloppy coding creating side effects

### Pattern 4: Syscall Bug Reproducer

From syscall path analysis:

```c
// user-space reproducer
#include <sys/ioctl.h>
#include <fcntl.h>

int main() {
    int fd = open("/dev/some_device", O_RDWR);

    // Trigger bug with specific ioctl parameters
    // From crash analysis: ioctl(fd, BUGGY_CMD, buggy_param)
    ioctl(fd, 0xdeadbeef, 0xffffffff);

    close(fd);
}
```

### Pattern 5: Memory Pressure / OOM

From OOM analysis:

```c
// user-space memory stress
#include <stdlib.h>
#include <string.h>

int main() {
    size_t size = 1024 * 1024 * 1024;  // 1GB per iteration
    void *ptr;

    while (1) {
        ptr = malloc(size);
        if (ptr) memset(ptr, 1, size);  // Force allocation
        // Keep allocating until OOM
    }
}
```

## Integration with Other Skills

This skill works best after other analysis skills have completed:

- **Use `/vmcore-analyzer` first** → Get root cause and crash details
- **Use `/lock-analyzer` if lock issue** → Get lock contention details
- **Use `/rag-case-retrieval`** → Check historical similar cases

**Workflow:**
```
User reports kernel problem
  ↓
/vmcore-analyzer (analyze vmcore)
  ↓
/lock-analyzer (if lock-related)
  ↓
/rag-case-retrieval (search similar cases)
  ↓
/kernel-testcase-generator (generate reproducer)
  ↓
Output: reproducer code + self-verification
  ↓
Test expert runs full verification
```

## Important Rules

### 🔴🔴🔴 Core Principle: Trigger Expected Bug, Avoid Side Effects

**The most important rule:**

- **Trigger the EXPECTED bug** - Reproducer should reliably trigger the specific bug identified in the analysis (e.g., deadlock, race condition, specific NULL dereference)
- **AVOID coding errors** - Don't introduce additional bugs due to sloppy coding (e.g., random NULL dereferences, uninitialized variables, wrong API usage)

**Examples of what to DO vs what NOT to do:**

| Bug Type | ✅ DO (Trigger Expected Bug) | ❌ DON'T (Avoid Side Effects) |
|----------|------------------------------|----------------------------|
| **Deadlock** | Two threads acquiring mutexes in reverse order as analyzed | Random mutex usage without clear pattern |
| **Race Condition** | Multiple threads updating shared counter without lock as in analysis | Uninitialized thread structures causing random crash |
| **NULL Pointer** | Trigger specific NULL dereference at identified location (e.g., `file->private_data` not initialized in open) | Random NULL dereference anywhere in code |
| **Memory Leak** | Leak memory in specific subsystem as analyzed | Random memory leaks in unrelated code paths |

**How to implement correctly:**

```c
// ✅ CORRECT: Trigger expected NULL pointer at analyzed location
static long device_ioctl_handler(struct file *file, unsigned int cmd, unsigned long arg) {
    struct my_device *dev = file->private_data;  // NULL because open() didn't set it
    return dev->ops->ioctl(dev, cmd, arg);  // Expected crash at this exact line
}

// ❌ WRONG: Random NULL pointer from coding error
static int helper_function(void) {
    struct device *dev = NULL;  // Random NULL, not from analysis
    return dev->some_field;  // This is a coding error, not reproducing the bug
}
```

**Self-check before writing code:**
- "Is this triggering the exact bug from analysis?" → YES
- "Am I introducing random bugs from sloppy coding?" → NO
- "Does the crash location match the analysis?" → YES

---

1. **Always ask for output directory** if not specified
2. **Read all provided analysis results** before coding
3. **Choose reproducer type based on problem characteristics**, not user preference
4. **Focus on triggering conditions** from analysis, not generic tests
5. **Self-verification is minimal**: compilation + basic load/run
6. **Don't do full testing**: Test expert handles that
7. **If verification fails, fix immediately and re-verify**
8. **Generate complete README** with build/run instructions
9. **Include debug tips** for test expert reference

## Output Requirements

When reproducer is complete:

1. Save all files to output directory
2. Run self-verification and log results
3. Report to user:
   - Output directory path
   - Reproducer type chosen
   - Verification status (pass/fail)
   - Brief usage instructions
   - What test expert should verify next

## Example Usage

### Example 1: From vmcore analysis

```
User: Based on the vmcore analysis results (see attached), create a reproducer for this mutex deadlock issue.

Input: Vmcore analysis showing:
  - Two threads blocked on mutexes
  - Deadlock chain: thread1 holds mutexA waiting for mutexB, thread2 holds mutexB waiting for mutexA
  - Crash type: hung task

Skill Output:
  - Creates kernel module reproducer
  - Two kernel threads acquiring mutexes in reverse order
  - Self-verification: module loads, threads start, deadlock occurs (hung task detected)
  - Output saved to: ~/deadlock_reproducer/
```

### Example 2: From lock analysis

```
User: Use the lock analysis results to create a reproducer for the spinlock contention causing soft lockup.

Input: Lock analysis showing:
  - Spinlock held for >10s
  - Multiple CPUs spinning on same lock
  - Soft lockup messages in kernel log

Skill Output:
  - Creates kernel module reproducer
  - One thread holds spinlock and sleeps (invalid but triggers bug)
  - Other threads spin on same lock
  - Self-verification: module loads, soft lockup messages appear
  - Output saved to: ~/spinlock_softlockup_reproducer/
```

### Example 3: From syscall crash

```
User: Create a user program to reproduce the NULL pointer crash in ioctl handler.

Input: Vmcore analysis showing:
  - Crash in device ioctl handler
  - NULL pointer dereference when device not initialized
  - Syscall path: ioctl(fd, CMD_INIT, NULL) triggers bug

Skill Output:
  - Creates user program reproducer
  - Opens device, calls ioctl with NULL parameter
  - Self-verification: program runs, dmesg shows panic/crash
  - Output saved to: ~/ioctl_nullptr_reproducer/
```

## Tips for Effective Reproducers

1. **Minimal but precise**: Focus on exact triggering conditions, not comprehensive testing
2. **Clear trigger point**: Code should clearly show where bug is triggered
3. **Obvious failure symptom**: Crash/hang should be easily observable
4. **No unnecessary complexity**: Simple code is easier to debug
5. **Include comments**: Explain why each line is needed (based on analysis)
6. **Match original conditions**: Kernel version, config, hardware if relevant
7. **Self-contained**: All dependencies included, no external setup needed

## Handling Verification Failures

If self-verification fails:

1. **Compilation error**:
   - Fix code immediately
   - Check kernel headers/APIs match target kernel version
   - Re-run make/gcc

2. **Module load failure**:
   - Check kernel config (module support enabled?)
   - Check module dependencies
   - Fix init function if error in initialization
   - Re-test with insmod

3. **Unexpected behavior**:
   - Review code vs analysis results
   - Adjust triggering logic
   - Re-run basic verification

**After fixes, report to user:**
- What was wrong
- How it was fixed
- New verification status

**DO NOT:**
- Give up if first attempt fails
- Expect perfect reproduction in self-verification
- Run extensive debugging (test expert's job)
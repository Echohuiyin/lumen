---
name: kernel-test-validator
description: Validate kernel bug reproduction cases by compiling and testing in QEMU. Use when user mentions '复现验证', '复现用例', 'reproduce test', 'kernel test case', '验证复现', 'kernel validator', or provides patch/scripts to test kernel behavior. This skill acts as a kernel testing expert that validates reproduction cases and reports results back to kernel experts for iteration.
---

# Kernel Test Validator Skill

Validate kernel bug reproduction cases through automated compilation and testing workflow.

## Purpose

This skill acts as a **kernel testing expert** whose job is:
1. Receive reproduction cases from kernel experts
2. Compile kernels with the provided patches/configs
3. Test in QEMU virtual machines
4. Analyze results to determine if the bug reproduces
5. Provide structured feedback for iteration

## When to Use

Trigger this skill when:
- User provides a reproduction case (patch, test script, config changes)
- User asks to validate if a kernel bug can be reproduced
- User mentions "复现验证", "复现用例", "reproduce test case", "kernel validator"
- User wants to test kernel behavior with specific patches
- User asks for reproduction feedback for kernel experts

## Workflow Overview

```
Input: Reproduction Case
  ↓
Step 1: Parse & Validate Case
  ↓
Step 2: Compile Kernel (via kernel-build)
  ↓
Step 3: Test in QEMU (via qemu-test)
  ↓
Step 4: Analyze Results
  ↓
Output: Validation Report
```

## Reproduction Case Format

A reproduction case typically includes:

### Required Components
```yaml
case_id: "BUG-12345"
description: "Brief description of the bug to reproduce"
kernel_version: "6.6" or "openeuler-6.6"
architecture: "arm64" | "arm32" | "x86_64"

# At least one of these:
patches:
  - path: "patches/fix.patch"
    description: "Patch to apply"
configs:
  - CONFIG_XXX=y
  - CONFIG_YYY=m

# Test verification:
test_method: "script" | "command" | "boot-check"
test_script: "tests/test_bug.sh"  # optional
test_command: "dmesg | grep BUG"  # optional
expected_result: "Should see BUG message in dmesg"
```

### Supported Formats

**Format 1: Structured Case File**
```yaml
# reproduction_case.yaml
case_id: "JFFS2-001"
description: "JFFS2 filesystem corruption under stress"
architecture: arm64
patches:
  - jffs2_stress_test.patch
configs:
  - CONFIG_JFFS2_FS=m
  - CONFIG_DEBUG_FS=y
test_script: tests/jffs2_stress.sh
expected_result: "Should trigger JFFS2 corruption error"
timeout: 300
```

**Format 2: Inline Description**
```
复现用例：
- 补丁：添加 JFFS2 压力测试补丁（见 patches/jffs2_stress.patch）
- 配置：启用 CONFIG_JFFS2_FS=m, CONFIG_DEBUG_FS=y
- 测试：运行 tests/jffs2_stress.sh 脚本
- 预期：应触发 JFFS2 corruption 错误信息
- 架构：arm64
```

**Format 3: Minimal (Patch Only)**
```
Patch: patches/ub_crash.patch
Expected: Kernel should crash with UB panic message
```

## Step 1: Parse and Validate Case

### 1.1 Case Extraction

Parse user input to extract:
- Patches (file paths or inline diff)
- Config options
- Test scripts/commands
- Expected behavior
- Architecture and build parameters

### 1.2 Validation Checklist

Check for required components:
- [ ] Kernel source location or version
- [ ] At least one modification (patch/config)
- [ ] Test verification method defined
- [ ] Expected result clearly stated
- [ ] Architecture specified

If components missing, ask user:
```
Missing required information:
- Kernel source path/version: ?
- Architecture: ? (default: arm64)
- Test verification method: ? (script/command/boot-check)

Please provide these details.
```

### 1.3 Prepare Build Parameters

Map case parameters to build options:
```bash
# Extract configs
CONFIGS=$(echo "$configs" | sed 's/CONFIG_/CONFIG_/' | tr '\n' ' ')

# Determine architecture
ARCH="${architecture:-arm64}"

# Check if cross-compile needed
if [ "$(uname -m)" != "$ARCH" ]; then
    CROSS="--cross"
fi

# Determine defconfig
DEFCONFIG="${defconfig:-}"
if [ -z "$DEFCONFIG" ]; then
    # Auto-detect openeuler_defconfig
    if [ -f "arch/$ARCH/configs/openeuler_defconfig" ]; then
        DEFCONFIG="openeuler_defconfig"
    fi
fi
```

## Step 2: Compile Kernel

**Delegate to kernel-build skill.**

### 2.1 Apply Patches (Before Build)

```bash
# Apply patches to kernel source
for patch in "${patches[@]}"; do
    if [ -f "$patch" ]; then
        echo "Applying patch: $patch"
        git apply "$patch" || patch -p1 < "$patch"
        PATCH_STATUS="applied"
    else
        echo "ERROR: Patch not found: $patch"
        exit 1
    fi
done

# Record applied patches
git diff HEAD > applied_patches.diff
```

### 2.2 Invoke kernel-build

Use `/kernel-build` skill with extracted parameters:

```bash
/kernel-build $CONFIGS --arch $ARCH $CROSS --defconfig $DEFCONFIG --jobs $JOBS
```

Example invocation:
```
/kernel-build JFFS2_FS DEBUG_FS --arch arm64 --cross --jobs 32
```

### 2.3 Build Validation

After kernel-build completes, verify:
- [ ] Kernel image exists at `arch/$ARCH/boot/Image`
- [ ] Modules compiled (if configs include modules)
- [ ] No build errors in log
- [ ] Patches successfully integrated

If build fails:
```
Build Failed Report
===================
Patch: patches/jffs2_stress.patch
Error: Build error in fs/jffs2/super.c line 245

Kernel expert feedback:
- Patch integration failed
- Error location: fs/jffs2/super.c:245
- Possible cause: Missing dependency or incompatible kernel version

Suggest: Review patch compatibility with kernel version
```

## Step 3: Test in QEMU

**Delegate to qemu-test skill.**

### 3.1 Prepare Test Environment

```bash
# Determine test method
if [ -n "$test_script" ]; then
    TEST_METHOD="--script $test_script"
elif [ -n "$test_command" ]; then
    TEST_METHOD="--cmd '$test_command'"
else
    TEST_METHOD="--interactive"
fi

# Set timeout
TIMEOUT="${timeout:-300}"
```

### 3.2 Invoke qemu-test

Use `/qemu-test` skill:

```bash
/qemu-test --arch $ARCH $TEST_METHOD --timeout $TIMEOUT --log --output qemu_test_outputs/
```

Example invocations:
```
/qemu-test --arch arm64 --script tests/jffs2_stress.sh --timeout 300
/qemu-test --arch x86_64 --cmd "dmesg | grep BUG" --timeout 60
/qemu-test --arch arm64 --interactive
```

### 3.3 Collect Test Outputs

Verify outputs collected:
- [ ] `boot.log` - QEMU boot output
- [ ] `test_result.log` - Test execution results (if script ran)
- [ ] `kernel_image` - Kernel image used
- [ ] `initramfs.cpio.gz` - Initramfs created

## Step 4: Analyze Results

### 4.1 Result Analysis Framework

Compare observed behavior with expected result:

**Success Criteria**:
- Expected error message appears
- Expected crash/panic occurs
- Expected test output matches
- Expected behavior observed

**Failure Criteria**:
- No expected error/crash
- Test passes without triggering bug
- Unexpected behavior
- QEMU timeout without result

### 4.2 Analysis Process

```bash
# Load expected result
EXPECTED="${expected_result}"

# Analyze boot log
boot_log="qemu_test_outputs/boot.log"

# Check for expected patterns
if grep -q "$EXPECTED_PATTERN" "$boot_log"; then
    RESULT="REPRODUCED"
    EVIDENCE=$(grep "$EXPECTED_PATTERN" "$boot_log")
else
    RESULT="NOT_REPRODUCED"
    EVIDENCE="No expected pattern found"
fi

# If test script ran
if [ -f "qemu_test_outputs/test_result.log" ]; then
    test_log="qemu_test_outputs/test_result.log"
    test_exit=$(grep "exit code" "$test_log" | tail -1)

    # Interpret test exit code
    if [ "$test_exit" -eq 0 ]; then
        TEST_STATUS="PASS"
    else
        TEST_STATUS="FAIL"
    fi
fi
```

### 4.3 Classification Logic

| Observed | Expected | Classification |
|----------|----------|----------------|
| Error appears | Error expected | ✓ REPRODUCED |
| Crash occurs | Crash expected | ✓ REPRODUCED |
| Test fails | Test should fail | ✓ REPRODUCED |
| No error | Error expected | ✗ NOT REPRODUCED |
| Test passes | Test should fail | ✗ NOT REPRODUCED |
| Timeout | - | ⚠ TIMEOUT |

## Step 5: Generate Report

### 5.1 Report Template - Success Case

```markdown
# Kernel Bug Validation Report

## Case Information
- Case ID: {case_id}
- Description: {description}
- Architecture: {architecture}
- Kernel Version: {kernel_version}

## Validation Status
**✓ SUCCESSFULLY REPRODUCED**

## Validation Process

### Build Phase
- Patches Applied: {list of patches}
- Config Options: {list of configs}
- Build Time: {duration}
- Kernel Image: {path}

### Test Phase
- Test Method: {script/command/interactive}
- Test Duration: {duration}
- QEMU Output: {boot.log summary}

## Evidence of Reproduction
{key log excerpts showing the bug}

### Observed Behavior
{description of what was observed}

### Expected Behavior
{description of expected behavior}

## Conclusion
The bug was successfully reproduced using the provided case. The validation process is verified and can be used for further debugging.

## Artifacts
- Kernel Image: qemu_test_outputs/kernel_image
- Boot Log: qemu_test_outputs/boot.log
- Test Result: qemu_test_outputs/test_result.log
- Applied Patches: applied_patches.diff

---
Generated by kernel-test-validator skill
```

### 5.2 Report Template - Failure Case

```markdown
# Kernel Bug Validation Report

## Case Information
- Case ID: {case_id}
- Description: {description}
- Architecture: {architecture}
- Kernel Version: {kernel_version}

## Validation Status
**✗ VALIDATION FAILED**

## Verification Steps

### Step 1: Case Parsing
- Patches: {list}
- Configs: {list}
- Test Method: {method}
- Expected Result: {expected}

### Step 2: Kernel Compilation
- Build Status: {success/failure}
- Build Time: {duration}
- {If failed: build error details}

### Step 3: QEMU Testing
- Test Duration: {duration}
- Test Exit Status: {exit code}
- Timeout: {yes/no}

### Step 4: Result Analysis
- Expected Pattern: {pattern}
- Pattern Found: {yes/no}
- Observed Output: {summary}

## Failure Analysis

### Why Validation Failed
{root cause analysis}

### Key Observations
{what was actually observed vs expected}

### Potential Issues
{list of potential problems with the reproduction case}

## Recommendations for Kernel Expert

To improve the reproduction case, consider:

1. **Patch Issues**:
   - {specific patch compatibility concerns}
   - {missing dependencies}

2. **Config Issues**:
   - {missing config options}
   - {incorrect config values}

3. **Test Method Issues**:
   - {test script problems}
   - {test timeout too short}
   - {test conditions not triggering bug}

4. **Other Suggestions**:
   - {additional recommendations}

## Artifacts for Review
- Build Log: {build.log}
- Boot Log: qemu_test_outputs/boot.log
- Test Result: qemu_test_outputs/test_result.log (if exists)
- Applied Patches: applied_patches.diff

---
Please review the failure analysis and refine the reproduction case based on the recommendations above.
```

## Integration Patterns

### Pattern 1: Full Workflow (Build + Test)

```bash
# User provides complete case
/kernel-test-validator reproduction_case.yaml

# Skill executes:
1. Parse case → extract parameters
2. Apply patches → patch integration
3. /kernel-build → compile kernel
4. /qemu-test → run tests
5. Analyze → generate report
```

### Pattern 2: Build Only (Pre-built Kernel)

If kernel already built:
```bash
/kernel-test-validator --kernel arch/arm64/boot/Image test_script.sh

# Skill skips build:
1. Parse case
2. Skip build (use provided kernel)
3. /qemu-test → run tests
4. Analyze → generate report
```

### Pattern 3: Quick Validation

Minimal input:
```bash
/kernel-test-validator "Test JFFS2 panic with panic-on-oops"

# Skill uses defaults:
- Architecture: arm64 (default)
- Config: CONFIG_PANIC_ON_OOPS=y
- Test: Boot check for panic
- Expected: Kernel panic
```

## Report Output Location

Always save reports to structured directory:

```
validation_outputs/
├── {case_id}/
│   ├── report.md             # Validation report
│   ├── build/
│   │   ├── kernel_image      # Compiled kernel
│   │   ├── applied_patches.diff
│   │   ├── build.log
│   │   └── config.txt        # Final .config
│   ├── test/
│   │   ├── boot.log          # QEMU output
│   │   ├── test_result.log   # Test results
│   │   ├── initramfs.cpio.gz
│   │   └── test_script.sh    # Test script (if used)
│   └── artifacts/
│       ├── modules/          # Kernel modules (if any)
│       └── summary.txt       # Quick summary
```

## Error Handling

### Build Phase Errors

**Patch Application Failed**:
```
ERROR: Patch application failed
Patch: patches/test.patch
Error: git apply failed - patch does not match kernel version

Suggestions:
- Verify patch is generated for correct kernel version
- Check patch format (git diff vs diff -u)
- Ensure kernel source is clean (no previous patches)
```

**Build Compilation Failed**:
```
ERROR: Kernel build failed
Error location: fs/jffs2/super.c:245
Error message: undefined symbol 'jffs2_stress_init'

Suggestions:
- Patch may have missing dependencies
- Check if CONFIG options are correct
- Review patch compatibility with kernel version
```

### Test Phase Errors

**QEMU Boot Failed**:
```
ERROR: QEMU failed to boot kernel
Boot log: qemu_test_outputs/boot.log (last 50 lines)

Suggestions:
- Kernel may be incompatible with architecture
- Check kernel config for minimal boot requirements
- Verify initramfs is created correctly
```

**Test Timeout**:
```
WARNING: Test timeout reached
Timeout: 300 seconds
Partial output: qemu_test_outputs/boot.log

Suggestions:
- Increase timeout for complex tests
- Check if test is stuck waiting for input
- Verify test script doesn't hang
```

## Skill Dependencies

This skill requires and invokes:
- `/kernel-build` - For kernel compilation
- `/qemu-test` - For QEMU testing

**Workflow Order**:
1. This skill parses the case
2. Invokes `/kernel-build` to compile
3. Invokes `/qemu-test` to test
4. This skill analyzes results

**Never** invoke in reverse order or skip steps.

## Example Scenarios

### Example 1: Complete Validation Case

```yaml
# case.yaml
case_id: "JFFS2-CORRUPT-001"
description: "JFFS2 corruption under concurrent write stress"
architecture: arm64
patches:
  - patches/jffs2_stress.patch
configs:
  - CONFIG_JFFS2_FS=m
  - CONFIG_DEBUG_FS=y
  - CONFIG_PANIC_ON_OOPS=y
test_script: tests/jffs2_concurrent_write.sh
expected_result: "Kernel panic with JFFS2 corruption message"
timeout: 180
```

Invocation:
```
/kernel-test-validator case.yaml
```

Report (Success):
```
✓ REPRODUCED - Kernel panic triggered at fs/jffs2/write.c:89
Evidence: "JFFS2: corruption detected in node 0x1234"
Test Duration: 45 seconds
Build Time: 3m 20s
```

### Example 2: Minimal Patch Verification

```
/kernel-test-validator "Verify patches/ub_fix.patch causes UB crash on arm64"
```

Skill interprets:
- Patch: patches/ub_fix.patch
- Architecture: arm64
- Expected: UB crash
- Test: Boot check

### Example 3: Failure Case Feedback

Input case fails to reproduce:
```
/kernel-test-validator failing_case.yaml
```

Report (Failure):
```
✗ NOT REPRODUCED

Failure Analysis:
- Expected: "JFFS2 panic message"
- Observed: Clean boot, no errors

Recommendations:
1. CONFIG_JFFS2_FS=m may need CONFIG_JFFS2_FS_WRITEBUFFER=y
2. Test script timeout (60s) may be insufficient
3. Stress test needs concurrent threads > 1

Please revise case with these suggestions.
```

## Advanced Features

### Multi-Patch Handling

```bash
# Apply multiple patches in order
patches:
  - 0001_base_fix.patch
  - 0002_stress_test.patch
  - 0003_debug_output.patch

# Skill applies sequentially
for patch in patches; do
    git apply "$patch"
    echo "Patch $patch applied successfully"
done
```

### Config Dependency Resolution

If user provides configs, skill adds dependencies:
```bash
# User provides: CONFIG_JFFS2_FS=m
# Skill detects dependencies and adds:
CONFIG_JFFS2_FS=m
CONFIG_JFFS2_FS_WRITEBUFFER=y  # Dependency
CONFIG_FSI=y                   # Dependency
```

### Custom Defconfig Support

```yaml
defconfig: bcm2835_defconfig  # ARM32 Raspberry Pi
architecture: arm32
```

## Best Practices

### For Kernel Experts (Input Side)

Provide clear reproduction cases:
1. Specify kernel version explicitly
2. List all required configs including dependencies
3. Define expected result precisely (error message pattern)
4. Provide standalone test scripts
5. Include timeout estimates

### For This Skill (Output Side)

Generate actionable reports:
1. Clear success/failure status
2. Evidence excerpts from logs
3. Specific failure reasons (not generic)
4. Concrete suggestions (config values, timeout numbers)
5. All artifacts saved for review

## Summary

This skill bridges kernel experts and testing validation:
- **Input**: Reproduction case from kernel expert
- **Output**: Validated result + actionable feedback
- **Tools**: kernel-build + qemu-test
- **Goal**: Iterate until bug reproduces

Use this skill to validate reproduction cases and provide structured feedback for kernel expert iteration.

// crash_stack_overflow.c - Stack overflow fault injection
// Triggers stack overflow via deep recursion

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Analysis-SKILL");
MODULE_DESCRIPTION("Fault injection: Stack overflow via recursion");

// Recursive function to overflow stack
static noinline int recursive_overflow(int depth)
{
	char buffer[256]; // Waste stack space

	// Touch buffer to prevent optimization
	memset((void *)buffer, depth & 0xFF, sizeof(buffer));

	printk(KERN_INFO "Recursion depth: %d\n", depth);

	// Continue recursion until stack overflow (intentional infinite recursion for testing)
	// NOLINTNEXTLINE: infinite-recursion - intentional for fault injection testing
	return recursive_overflow(depth + 1);
}

static int __init crash_stack_overflow_init(void)
{
	printk(KERN_INFO "=== Stack Overflow Test ===\n");
	printk(KERN_INFO "Starting deep recursion to overflow kernel stack...\n");

	// Start recursion - will overflow stack after ~100-200 iterations
	// Kernel stack is typically 8KB-16KB
	recursive_overflow(0);

	// Never reaches here
	printk(KERN_INFO "Stack overflow complete (should never reach)\n");
	return 0;
}

static void __exit crash_stack_overflow_exit(void)
{
	printk(KERN_INFO "crash_stack_overflow: cannot exit (stack overflow)\n");
}

module_init(crash_stack_overflow_init);
module_exit(crash_stack_overflow_exit);
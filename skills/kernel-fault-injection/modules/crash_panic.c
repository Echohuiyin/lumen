// crash_panic.c - Direct kernel panic fault injection
// Triggers immediate kernel panic

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/kernel.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Analysis-SKILL");
MODULE_DESCRIPTION("Fault injection: Direct kernel panic");

static int __init crash_panic_init(void)
{
	printk(KERN_INFO "=== Direct Kernel Panic Test ===\n");
	printk(KERN_INFO "Calling panic() directly...\n");

	// Direct panic call
	panic("Analysis-SKILL: intentional kernel panic for vmcore generation");

	// Never reaches here
	return 0;
}

static void __exit crash_panic_exit(void)
{
	printk(KERN_INFO "crash_panic: cannot exit (kernel already panicked)\n");
}

module_init(crash_panic_init);
module_exit(crash_panic_exit);
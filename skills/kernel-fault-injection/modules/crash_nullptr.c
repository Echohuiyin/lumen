// crash_nullptr.c - NULL pointer dereference fault injection
// Triggers kernel panic via NULL pointer write

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Analysis-SKILL");
MODULE_DESCRIPTION("Fault injection: NULL pointer dereference");

static int __init crash_nullptr_init(void)
{
	int *ptr = NULL;

	printk(KERN_INFO "=== NULL Pointer Dereference Test ===\n");
	printk(KERN_INFO "Triggering NULL pointer write...\n");

	// NULL pointer write - triggers Oops -> Panic (with PANIC_ON_OOPS)
	*ptr = 42;

	// Never reaches here
	printk(KERN_INFO "This should never print\n");
	return 0;
}

static void __exit crash_nullptr_exit(void)
{
	printk(KERN_INFO "crash_nullptr: module exit (should never reach)\n");
}

module_init(crash_nullptr_init);
module_exit(crash_nullptr_exit);
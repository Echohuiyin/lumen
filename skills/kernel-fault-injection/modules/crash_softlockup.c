// crash_softlockup.c - Soft lockup fault injection
// Triggers soft lockup via infinite loop with interrupts disabled

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/interrupt.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Analysis-SKILL");
MODULE_DESCRIPTION("Fault injection: CPU soft lockup");

static int __init crash_softlockup_init(void)
{
	printk(KERN_INFO "=== Soft Lockup Test ===\n");
	printk(KERN_INFO "Disabling interrupts and entering infinite loop...\n");
	printk(KERN_INFO "Watchdog will detect CPU stuck for >22s\n");

	// Disable interrupts - prevents scheduler from running
	local_irq_disable();

	// Infinite loop - CPU stuck forever
	// CONFIG_BOOTPARAM_SOFTLOCKUP_PANIC=y will trigger panic after ~22s
	while (1) {
		// Spin forever
		asm volatile("nop");
	}

	// Never reaches here
	return 0;
}

static void __exit crash_softlockup_exit(void)
{
	// Cannot exit - CPU is stuck
	printk(KERN_INFO "crash_softlockup: cannot exit\n");
}

module_init(crash_softlockup_init);
module_exit(crash_softlockup_exit);
// crash_deadlock.c - Mutex ABBA deadlock fault injection
// Triggers hung task panic by making init function participate in deadlock
// KEY: init function blocks -> insmod process in D state -> hung_task detects

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/mutex.h>
#include <linux/kthread.h>
#include <linux/delay.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Analysis-SKILL");
MODULE_DESCRIPTION("Fault injection: Mutex ABBA deadlock (init participates)");

static DEFINE_MUTEX(mutex_a);
static DEFINE_MUTEX(mutex_b);

static struct task_struct *thread2;

// Thread 2: lock B -> try lock A (reverse order - deadlock!)
static int thread2_fn(void *data)
{
	printk(KERN_INFO "Thread 2: trying to lock B\n");
	mutex_lock(&mutex_b);
	printk(KERN_INFO "Thread 2: locked B, trying to lock A\n");

	msleep(500); // Give init time to lock A

	printk(KERN_INFO "Thread 2: trying to lock A (will block - DEADLOCK)\n");
	mutex_lock(&mutex_a); // Blocked - init holds A, wants B

	printk(KERN_INFO "Thread 2: locked A (should never reach)\n");
	mutex_unlock(&mutex_a);
	mutex_unlock(&mutex_b);

	return 0;
}

static int __init crash_deadlock_init(void)
{
	printk(KERN_INFO "=== Mutex ABBA Deadlock Test ===\n");
	printk(KERN_INFO "Init function participates in deadlock (insmod will block)\n");
	printk(KERN_INFO "Hung task will detect insmod in D state and panic\n");

	// Step 1: init locks A first
	printk(KERN_INFO "Init: trying to lock A\n");
	mutex_lock(&mutex_a);
	printk(KERN_INFO "Init: locked A\n");

	// Step 2: create thread2 that will lock B
	thread2 = kthread_run(thread2_fn, NULL, "deadlock_thread2");
	printk(KERN_INFO "Init: created thread2\n");

	// Step 3: wait for thread2 to lock B
	msleep(1000);

	// Step 4: init tries to lock B -> DEADLOCK
	// init (insmod process) will be in D state -> hung_task detects!
	printk(KERN_INFO "Init: trying to lock B (will block - DEADLOCK)\n");
	mutex_lock(&mutex_b); // Blocked - thread2 holds B

	// Never reaches here
	printk(KERN_INFO "Init: locked B (should never reach)\n");
	mutex_unlock(&mutex_b);
	mutex_unlock(&mutex_a);

	return 0; // Never returns
}

static void __exit crash_deadlock_exit(void)
{
	// Cannot exit - init is deadlocked
	printk(KERN_INFO "crash_deadlock: cannot exit (init deadlocked)\n");
}

module_init(crash_deadlock_init);
module_exit(crash_deadlock_exit);
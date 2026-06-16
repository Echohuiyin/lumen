# Makefile for kernel fault injection modules
# Usage: make ARCH=<arch> CROSS_COMPILE=<prefix> KDIR=<kernel_dir>

obj-m += crash_nullptr.o
obj-m += crash_softlockup.o
obj-m += crash_deadlock.o
obj-m += crash_panic.o
obj-m += crash_stack_overflow.o

# Disable infinite recursion warning for stack_overflow module (intentional for fault injection)
ccflags-y := -Wno-infinite-recursion

# Default kernel directory
KDIR ?= /lib/modules/$(shell uname -r)/build

all: modules

modules:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

# Install to specified directory
install:
	mkdir -p $(INSTALL_DIR)
	cp *.ko $(INSTALL_DIR)/

.PHONY: all modules clean install
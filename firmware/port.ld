/* Provisional locations for analysis builds only. Ownership must be established
 * on the target before this payload may be installed. Keep stock SRAM untouched. */
ENTRY(v119_init_wrapper)
MEMORY {
    FLASH (rx) : ORIGIN = 0x08080000, LENGTH = 128K
    CCM (rw) : ORIGIN = 0x10000000, LENGTH = 16K
}
SECTIONS {
    .text : {
        KEEP(*(.text.v119_*))
        *(.text*) *(.rodata*)
        . = ALIGN(4);
    } > FLASH
    .ARM.extab : { *(.ARM.extab*) } > FLASH
    .ARM.exidx : { *(.ARM.exidx*) } > FLASH
    .data : {
        . = ALIGN(4); __data_start = .;
        *(.data*)
        . = ALIGN(4); __data_end = .;
    } > CCM AT> FLASH
    __data_load = LOADADDR(.data);
    .bss (NOLOAD) : {
        . = ALIGN(8); __bss_start = .;
        *(.bss*) *(COMMON)
        . = ALIGN(8); __bss_end = .;
    } > CCM
    /DISCARD/ : { *(.comment) *(.note*) }
    ASSERT(__bss_end <= ORIGIN(CCM) + LENGTH(CCM), "CCM budget exceeded")
}

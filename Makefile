CC ?= cc
PYTHON ?= python3
CROSS ?= arm-none-eabi-
CFLAGS_HOST = -std=c11 -O2 -Wall -Wextra -Werror -Isrc
CFLAGS_ARM = -std=c11 -Os -g3 -Wall -Wextra -Werror -Isrc -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard -ffunction-sections -fdata-sections -fno-common -fno-strict-aliasing
ARM_OBJ = build/tx_dsp.o build/tx_protocol.o build/tx_interpolation.o build/adapter.o build/runtime.o build/hooks.o
TEST_ARM_OBJ = $(filter-out build/adapter.o,$(ARM_OBJ)) build/adapter-test.o
.PHONY: all test test-hooks test-adapter test-adapter-test test-runtime payload test-payload clean
all: test
build:
	mkdir -p build
build/tx_dsp.o: src/tx_dsp.c src/tx_dsp.h src/tx_math.h | build
	$(CROSS)gcc $(CFLAGS_ARM) -c $< -o $@
build/tx_protocol.o: src/tx_protocol.c src/tx_protocol.h src/tx_dsp.h | build
	$(CROSS)gcc $(CFLAGS_ARM) -c $< -o $@
build/tx_interpolation.o: firmware/tx_interpolation.c firmware/tx_interpolation.h | build
	$(CROSS)gcc $(CFLAGS_ARM) -c $< -o $@
build/adapter.o: firmware/adapter.c firmware/cycle_profile.h firmware/tx_interpolation.h src/tx_dsp.h src/tx_protocol.h | build
	$(CROSS)gcc $(CFLAGS_ARM) -c $< -o $@
build/adapter-test.o: firmware/adapter.c firmware/cycle_profile.h firmware/tx_interpolation.h src/tx_dsp.h src/tx_protocol.h | build
	$(CROSS)gcc $(CFLAGS_ARM) -DBB_TEST_BUILD=1 -c $< -o $@
build/runtime.o: firmware/runtime.c | build
	$(CROSS)gcc $(CFLAGS_ARM) -ffreestanding -c $< -o $@
build/hooks.o: firmware/hooks.S | build
	$(CROSS)gcc $(CFLAGS_ARM) -c $< -o $@
build/tx-port.elf: $(ARM_OBJ) firmware/port.ld
	$(CROSS)gcc $(CFLAGS_ARM) -nostdlib -Wl,--gc-sections,-Map=build/tx-port.map -Tfirmware/port.ld $(ARM_OBJ) -Wl,--start-group -lm -lgcc -Wl,--end-group -o $@
build/tx-test.elf: $(TEST_ARM_OBJ) firmware/port.ld
	$(CROSS)gcc $(CFLAGS_ARM) -nostdlib -Wl,--gc-sections,-Map=build/tx-test.map -Tfirmware/port.ld $(TEST_ARM_OBJ) -Wl,--start-group -lm -lgcc -Wl,--end-group -o $@
payload: build/tx-port.elf
	$(CROSS)size $<
	@echo 'Analysis ELF only: no installable firmware is generated.'
test-payload: build/tx-test.elf
	$(CROSS)size $<
	@echo 'TEST payload: supported GUI activation required; hardware validation pending.'
test: | build
	$(CC) $(CFLAGS_HOST) src/tx_dsp.c tests/test_tx_dsp.c -lm -o build/test_tx_dsp
	./build/test_tx_dsp
	$(CC) $(CFLAGS_HOST) src/tx_dsp.c src/tx_protocol.c tests/test_protocol.c -lm -o build/test_protocol
	./build/test_protocol
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'
test-hooks:
	$(PYTHON) tools/verify_hooks.py --cross $(CROSS)
test-adapter: build/tx-port.elf
	$(PYTHON) tools/verify_adapter.py
test-adapter-test: build/tx-test.elf
	$(PYTHON) tools/verify_adapter.py --elf build/tx-test.elf --test-build
test-runtime: build/tx-port.elf
	$(PYTHON) tools/verify_runtime.py --elf build/tx-port.elf
clean:
	rm -rf build

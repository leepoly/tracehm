import os, sys
import random
import flatmem

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 %s traceoutput" % sys.argv[0])
        sys.exit(0)
    n_access = 200
    cnt = 0
    with open(sys.argv[1], 'w+') as tracefile:
        for i in range(n_access):
            set_i = 1
            region_i = random.randint(0, 8)
            # region_i = random.randint(0, (1<<flatmem.addr_region_bit) - 1)
            is_write_i = random.randint(0, 1)
            addr_i = flatmem.make_address(set_i, region_i, 0)
            tracefile.write("%d\t0x%x\t%x\n" % (cnt, addr_i, is_write_i))
            cnt += 1


import os, sys
import random
import flatmem

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python3 %s traceinput traceoutput" % sys.argv[0])
        sys.exit(0)
    n_access = 200
    cnt = 0
    cnt2 = 0
    trace_in = open(sys.argv[1], 'r')
    trace_out = open(sys.argv[2], 'w+')
    for line in trace_in:
        cnt2 += 1
        try:
            arr = line.split('\t')
            addr = int(arr[1], base=16)
            set_i = flatmem.extract_bit(addr, flatmem.addr_set_low, flatmem.addr_set_bit)
            is_write = (int(arr[2]) == 1)
        except (ValueError, IndexError):
            print(cnt2)
            continue
        if set_i == 1:
            trace_out.write("%d\t0x%x\t%x\n" % (cnt, addr, is_write))
            cnt += 1

    trace_in.close()
    trace_out.close()


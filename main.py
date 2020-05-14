import os, sys
import flatmem

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 %s tracefile" % sys.argv[0])
        os.exit(0)
    memory = flatmem.init()
    with open(sys.argv[1]) as tracefile:
        for line in tracefile:
            arr = line.split('\t')
            addr = int(arr[1], base=16)
            is_write = (int(arr[2]) == 1)
            memory.access(addr, is_write)

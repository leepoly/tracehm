import os, sys
import flatmem

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 %s tracefile" % sys.argv[0])
        sys.exit(0)
    memoryctl = flatmem.FlatController()
    with open(sys.argv[1]) as tracefile:
        for line in tracefile:
            arr = line.split('\t')
            addr = int(arr[1], base=16)
            is_write = (int(arr[2]) == 1)
            new_event = flatmem.MemEvent(addr, is_write, 0)
            memoryctl.access(new_event)
    memoryctl.showstats()

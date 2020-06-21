import os, sys
import flatmem

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 %s tracefile [config1=value1]" % sys.argv[0])
        sys.exit(0)
    memoryctl = flatmem.FlatController()
    modified_configs = {}
    if len(sys.argv) == 3:
        modified_configs = dict([arg.split('=', maxsplit=1) for arg in sys.argv[2:]])
    memoryctl.set_config(modified_configs)
    with open(sys.argv[1]) as tracefile:
        for line in tracefile:
            arr = line.split('\t')
            addr = int(arr[1], base=16)
            is_write = (int(arr[2]) == 1)
            new_event = flatmem.MemEvent(addr, is_write, 0)
            memoryctl.access(new_event)
    memoryctl.print_config()
    memoryctl.showstats()

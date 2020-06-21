from enum import Enum
import random
import types

class TimingObj(object):
    avail_cycle = 0

class MemEvent(object):
    def __init__(self, p_address, is_write, current_cycle, is_migration = False):
        self.p_addr = p_address
        self.m_addr = p_address
        self.is_write = is_write
        self.is_migration = is_migration
        self.current_cycle = current_cycle
class SwapPolicy(Enum):
    FastSwap = 0
    SlowSwap = 1
    SmartSwap = 2
    NoSwap = 3
class BypassPolicy(Enum):
    Never = 0
    Probability = 1
class ReplPolicy(Enum):
    Random = 0
    LRU = 1 # LRULIP
    LRULIP = 2
    LFU = 3

addr_bit = 48
addr_page_low = 12
addr_page_bit = addr_bit - addr_page_low
addr_offset_low = 0
addr_offset_bit = 12
addr_region_low = 12
addr_region_bit = 4
addr_set_low = addr_region_low + addr_region_bit
addr_set_bit = addr_bit - addr_set_low
INF = 1000000000
c_trans_cache_capacity_per_set = 4

class Memory(TimingObj):
    access_cnt = 0
    def __init__(self, capacity, read_lat, write_lat, name = "memory"):
        self.capacity = capacity
        self.read_lat = read_lat
        self.write_lat = write_lat
        self.name = name
        self.used_cycle = 0
    def request(self, event):
        # print("addr:%x capacity:%x" % (event.m_addr, self.capacity))
        if event.m_addr > self.capacity:
            print("[Error] Out of %s %x>%x!" % (self.name, event.m_addr, self.capacity))
            return -1 # out of memory exception
        if event.is_write:
            self.avail_cycle = max(self.avail_cycle, event.current_cycle) + self.write_lat
            self.used_cycle += self.write_lat
        else:
            self.avail_cycle = max(self.avail_cycle, event.current_cycle) + self.read_lat
            self.used_cycle += self.read_lat
        event.current_cycle = self.avail_cycle
        if not event.is_migration:
            self.access_cnt += 1
            # print("[info] Access %s  %x" % (self.name, event.m_addr))

def extract_bit(value, start, len):
    tmp = value >> start
    mask = (1<<len) - 1
    # print("extract value%x start%d len%d res%x" % (value, start, len, tmp & mask))
    return tmp & mask

def make_address(addr_set, addr_region, addr_offset):
    address = addr_set
    address = address << addr_region_bit | addr_region
    address = address << addr_offset_bit | addr_offset
    return address

class FlatMemory(TimingObj):
    trans_table = {} # in fastmem. p_page -> m_page
    uncached_fast_trans_num = 0
    cached_fast_trans_num = 0

    def trans_table_remove(self, page):
        if page in self.trans_table:
            del self.trans_table[page]

    def __init__(self, flatconfig):
        self.fastmem = Memory(flatconfig["fast_cap"], flatconfig["fast_read_lat"], flatconfig["fast_write_lat"], "fastmem")
        self.slowmem = Memory(flatconfig["slow_cap"], flatconfig["slow_read_lat"], flatconfig["slow_write_lat"], "slowmem")
        self.trans_table_read_lat = flatconfig["fast_read_lat"]
        self.fast_block = flatconfig["fast_block"]

    def mpage_in_fastmem(self, maddress):
        region = extract_bit(maddress, addr_region_low-addr_page_low, addr_region_bit)
        return region < self.fast_block

    def maddr_in_fastmem(self, maddress):
        region = extract_bit(maddress, addr_region_low, addr_region_bit)
        return region < self.fast_block

    def paddr_in_fastmem(self, paddress):
        p_page = extract_bit(paddress, addr_page_low, addr_page_bit)
        m_page = self.trans_table.get(p_page, p_page) # default=p_page
        return self.mpage_in_fastmem(m_page)

    def ppage_in_fastmem(self, ppage):
        m_page = self.trans_table.get(ppage, ppage) # default=p_page
        return self.mpage_in_fastmem(m_page)

    def translate_address(self, paddress):
        p_page = extract_bit(paddress, addr_page_low, addr_page_bit)
        p_offset = extract_bit(paddress, addr_offset_low, addr_offset_bit)
        m_page = self.trans_table.get(p_page, p_page) # default=p_page
        m_address = (m_page << addr_page_low) | p_offset
        # print("translate paddr%x maddr%x" % (paddress, m_address))
        return m_address

    def translate_page_inv(self, ppage):
        if not ppage in self.trans_table:
            # ppage is not swapped. The inverted page is itself
            return ppage
        for (ppage_i, mpage_i) in self.trans_table.items():
            if mpage_i == ppage:
                return ppage_i

    def sync_cycle(self):
        self.avail_cycle = max(self.fastmem.avail_cycle, self.slowmem.avail_cycle)
        self.fastmem.avail_cycle = self.slowmem.avail_cycle = self.avail_cycle # we take a serialization timing model so far

    def advance_cycle(self, is_fastmem, cycle):
        if is_fastmem:
            self.fastmem.avail_cycle = max(self.fastmem.avail_cycle, self.avail_cycle) + cycle
            self.fastmem.used_cycle += cycle
        else:
            self.slowmem.avail_cycle = max(self.slowmem.avail_cycle, self.avail_cycle) + cycle
            self.slowmem.used_cycle += cycle
        self.avail_cycle = max(self.fastmem.avail_cycle, self.slowmem.avail_cycle)

    def trans_table_set(self, new_ppage, new_mpage):
        if new_ppage == new_mpage:
            if new_ppage in self.trans_table:
                del self.trans_table[new_ppage]
            return
        self.trans_table[new_ppage] = new_mpage

    def request(self, event):
        event.m_addr = self.translate_address(event.p_addr)
        in_fast = self.maddr_in_fastmem(event.m_addr)
        # print("granted access %x -> %x in_fast %x" % (event.p_addr, event.m_addr, in_fast))
        if in_fast:
            self.fastmem.request(event)
        else:
            self.slowmem.request(event)
        self.sync_cycle()

# MetaCaches are in the unit of set. They are usually put in SRAM.
# They store the cache of trans_table for better performance. They also monitor hotness of blocks (by their region id of paddr, not maddr).
# They are used by FlatController to emit advanced operation (swap, duplicate, ...)
class CacheEntry(object):
    hotness = 0
    def __init__(self, hotness):
        self.hotness = hotness

class MetaCache(TimingObj):
    set_id = 0
    def __init__(self, set_id, flatmem):
        self.set_id = set_id
        self.flatmem = flatmem
        self.timestamp = 0 # for ReplPolicy.LRU or ReplPolicy.LRULIP

    entries = {} # region_id -> hotness
    cached_trans_table = [] # List of pages. we do not actually duplicate transtable. Use a bool array to cancel latency for cached mapping.

    def trans_cache_remove(self, page):
        if self.cached_trans_table.count(page):
            self.cached_trans_table.remove(page)

    def track_hotness(self, event, repl_policy):
        # update global registers
        if repl_policy == ReplPolicy.LRU or repl_policy == ReplPolicy.LRULIP:
            self.timestamp += 1
        new_entry = False
        p_region = extract_bit(event.p_addr, addr_region_low, addr_region_bit)
        # create new entry
        if not p_region in self.entries:
            if repl_policy == ReplPolicy.LRU or repl_policy == ReplPolicy.LRULIP:
                self.entries[p_region] = CacheEntry(0)
                new_entry = True
            elif repl_policy == ReplPolicy.LFU:
                self.entries[p_region] = CacheEntry(0)
            elif repl_policy == ReplPolicy.Random:
                self.entries[p_region] = CacheEntry(random.randint(1, (1 << addr_set_bit) ** 3))
        # update existing entry
        if repl_policy == ReplPolicy.LFU:
            self.entries[p_region] = CacheEntry(self.entries[p_region].hotness + 1)
        elif repl_policy == ReplPolicy.LRU:
            self.entries[p_region] = CacheEntry(self.timestamp)
        elif repl_policy == ReplPolicy.LRULIP and (not new_entry):
            self.entries[p_region] = CacheEntry(self.timestamp)
        # print("debug region:%x hotness:%d" % (p_region, self.entries[p_region].hotness))

    def access_trans_cache(self, p_addr):
        p_page = extract_bit(p_addr, addr_page_low, addr_page_bit)
        # print(self.cached_trans_table)
        if not p_page in self.cached_trans_table:
            self.flatmem.uncached_fast_trans_num += 1
            # print("trans_table cache miss add 1 cycle")
            self.flatmem.advance_cycle(True, self.flatmem.trans_table_read_lat) # if miss, add translation latency
            self.flatmem.sync_cycle()
            self.cached_trans_table.append(p_page)
            if len(self.cached_trans_table) > c_trans_cache_capacity_per_set:
                self.cached_trans_table.pop(0) # LRU replacement is used. pop the first element
        # if hit, no latency added
        else:
            self.flatmem.cached_fast_trans_num += 1
            self.cached_trans_table.remove(p_page)
            self.cached_trans_table.append(p_page)
        return self.flatmem.translate_address(p_addr)

    def find_victim(self, event):
        min_hotness = INF
        min_hotness_region = -1
        for region_id, item in self.entries.items():
            p_addr = make_address(self.set_id, region_id, 0)
            if self.flatmem.paddr_in_fastmem(p_addr):
                if item.hotness > INF:
                    print("[warning] hotness over INF")
                if item.hotness < min_hotness:
                    min_hotness = item.hotness
                    min_hotness_region = region_id
        if min_hotness_region != -1:
            return min_hotness_region
        return -1

    def get_hotness_rank(self):
        # return self.entries
        sorted_list = sorted(self.entries.items(), key = lambda item: item[1].hotness)
        hotness_list = list(map(lambda item: item[0], sorted_list))
        # print(hotness_list)
        return hotness_list

flat_config1 = {
    "fast_cap": 0x1003fff, # 16KB, 4 blocks * 15 sets
    "slow_cap": 0x100ffff, # 128KB, 14 blocks * 15 sets
    "fast_read_lat": 1,
    "fast_write_lat": 1,
    "slow_read_lat": 2,
    "slow_write_lat": 2,
    "fast_block": 4,
    "swap_policy": SwapPolicy.SmartSwap,
    "bypass_policy": BypassPolicy.Probability,
    "bypass_probability": 0.5,
    "repl_policy": ReplPolicy.LRU,
}

flat_config_dram_nvm = {
    "fast_cap": 0x1001fff, # 16KB, 2 blocks * 15 sets
    "slow_cap": 0x100ffff, # 128KB, 14 blocks * 15 sets
    "fast_read_lat": 1,
    "fast_write_lat": 1,
    "slow_read_lat": 5,
    "slow_write_lat": 10,
    "fast_block": 2,
    "swap_policy": SwapPolicy.SlowSwap,
    "bypass_policy": BypassPolicy.Probability,
    "bypass_probability": 0.5,
    "repl_policy": ReplPolicy.LRU,
}

class SmartSwap(object):
    swap_alpha = 3.0 # benefit of relative rank
    swap_beta = 6 # cost of one migration
    swap_gamma = 1.0 # benefit of one empty slot: 1.0
    slow_mru_region = -1
    fast_region = [] # head is the LRU while tail is the MRU
    def __init__(self, rank_list, flatmem, set_id):
        self.rank_list = rank_list # head is the LRU while tail is the MRU
        self.flatmem = flatmem
        self.set_id = set_id
        self.fast_region = []
        for pregion in self.rank_list:
            ppage = extract_bit(make_address(set_id, pregion, 0), addr_page_low, addr_page_bit)
            is_fast = self.flatmem.ppage_in_fastmem(ppage)
            if (not is_fast):
                self.slow_mru_region = pregion
            elif (is_fast):
                self.fast_region.append(pregion)

    def search_region_in_rank(self, page):
        for i in range(len(self.rank_list)):
            if self.rank_list[i] == page:
                return i # return the rank
        return -1

    def find_best_restore_choice(self):
        max_util = -1
        best_src = best_dst = -1
        for pregion in self.fast_region:
            ppage = extract_bit(make_address(self.set_id, pregion, 0), addr_page_low, addr_page_bit)
            ppage_prev = self.flatmem.translate_page_inv(ppage)
            if ppage_prev != ppage:
                pregion_prev = extract_bit(ppage_prev, addr_region_low, addr_region_bit)
                ppage_rank = self.search_region_in_rank(pregion)
                ppage_prev_rank = self.search_region_in_rank(pregion_prev)

                if (self.swap_alpha * (ppage_prev_rank - ppage_rank) + self.swap_gamma - self.swap_beta) > max_util:
                    max_util = self.swap_alpha * (ppage_prev_rank - ppage_rank) + self.swap_gamma - self.swap_beta
                    best_src, best_dst = ppage, ppage_prev
        return (max_util, best_src, best_dst)

    def get_repl_util(self):
        # swap most inactive fast and most active slowblock
        # we use their LRU order as their rank order
        slow_rank = self.search_region_in_rank(self.slow_mru_region)
        fast_rank = self.search_region_in_rank(self.fast_region[0])
        repl_util = self.swap_alpha * (slow_rank - fast_rank) - self.swap_beta
        # print("repl get: %d %d %d" % (slow_rank, fast_rank, repl_util))
        # print("fast rank: ", self.fast_page)
        return (repl_util, self.slow_mru_region, self.fast_region[0])

class FlatController(TimingObj):
    metasets = {} # set_id -> MetaCache
    access_cnt = 0

    def __init__(self):
        self.config = flat_config1 # select default config
        self.flatmem = FlatMemory(self.config)

    def set_config(self, dic):
        for (k_i, v_i) in dic.items():
            if not k_i in self.config:
                print("[warning] ignore %s" % k_i)
                continue
            if k_i == "swap_policy":
                self.config["swap_policy"] = SwapPolicy[v_i]
            elif k_i == "bypass_policy":
                self.config["bypass_policy"] = BypassPolicy[v_i]
            elif k_i == "repl_policy":
                self.config["repl_policy"] = ReplPolicy[v_i]
            elif isinstance(self.config[k_i], int):
                self.config[k_i] = int(v_i)
            elif isinstance(self.config[k_i], float):
                self.config[k_i] = float(v_i)
            print("[info] change %s to %s" % (k_i, v_i))

        if self.config["swap_policy"] == SwapPolicy.SmartSwap:
            self.smart_swap_repl_cnt = 0
            self.smart_swap_restore_cnt = 0
        elif self.config["swap_policy"] == SwapPolicy.FastSwap:
            self.fast_swap_swap_cnt = 0
        elif self.config["swap_policy"] == SwapPolicy.SlowSwap:
            self.slow_swap_swap_cnt = 0

    def print_config(self):
        print("display all configs")
        for (k_i, v_i) in sorted(self.config.items()):
            print("\t%s = %s" % (k_i, v_i))

    def trig_monitor(self, event):
        in_fast = self.flatmem.paddr_in_fastmem(event.p_addr)
        if self.config["bypass_policy"] == BypassPolicy.Never:
            return not in_fast # migrate if access slowmem
        elif self.config["bypass_policy"] == BypassPolicy.Probability:
            if random.random() > self.config["bypass_probability"]:
                return False
            else:
                return not in_fast # migrate if access slowmem

    def sync_cycle(self):
        self.flatmem.sync_cycle()
        self.avail_cycle = max(self.avail_cycle, self.flatmem.avail_cycle)

    def gen_swap_event(self, p_addr1, p_addr2):
        self.flatmem.request(MemEvent(p_addr1, False, self.avail_cycle, is_migration=True))
        self.flatmem.sync_cycle()
        self.flatmem.request(MemEvent(p_addr2, False, self.avail_cycle, is_migration=True))
        self.flatmem.sync_cycle()
        self.flatmem.request(MemEvent(p_addr1, True, self.avail_cycle, is_migration=True))
        self.flatmem.sync_cycle()
        self.flatmem.request(MemEvent(p_addr2, True, self.avail_cycle, is_migration=True))
        self.flatmem.sync_cycle()

    def start_migration(self, p_addr1, p_addr2, swap_policy):
        infast_1 = self.flatmem.paddr_in_fastmem(p_addr1)
        infast_2 = self.flatmem.paddr_in_fastmem(p_addr2)
        # p_addr1 is victim page (in fastmem), p_addr2 is challenging page (in slowmem)
        assert(infast_1 ^ infast_2) # must be one fastblock and one slowblock
        p_page1 = extract_bit(p_addr1, addr_page_low, addr_page_bit)
        p_page2 = extract_bit(p_addr2, addr_page_low, addr_page_bit)
        set_id = extract_bit(p_addr1, addr_set_low, addr_set_bit) # p_addr1, p_addr2 must be in the same set
        if swap_policy == SwapPolicy.FastSwap:
            self.gen_swap_event(p_addr1, p_addr2)
            self.fast_swap_swap_cnt += 1
            m_addr1 = self.metasets[set_id].access_trans_cache(p_addr1)
            m_addr2 = self.metasets[set_id].access_trans_cache(p_addr2)
            m_page1 = extract_bit(m_addr1, addr_page_low, addr_page_bit)
            m_page2 = extract_bit(m_addr2, addr_page_low, addr_page_bit)
            # print("[info] p1 %x m1 %x  p2 %x m2 %x" % (p_addr1, m_addr1, p_addr2, m_addr2))
            self.flatmem.trans_table_set(p_page1, m_page2)
            self.flatmem.trans_table_set(p_page2, m_page1)
            # print("[info] migration done", self.flatmem.trans_table)
            # print("migration done %x(%x) <-> %x(%x)" % (p_addr1, self.flatmem.trans_table[p_page1], p_addr2, self.flatmem.trans_table[p_page2]))
        elif swap_policy == SwapPolicy.SlowSwap:
            # exception: when the challenger was originally in fastmem, swap challenger with trans[challenger]
            if self.flatmem.maddr_in_fastmem(p_addr2):
                p_addr1 = self.metasets[set_id].access_trans_cache(p_addr2)
                p_page1 = extract_bit(p_addr1, addr_page_low, addr_page_bit)

            m_addr1 = self.metasets[set_id].access_trans_cache(p_addr1) # check whether fastblock is not swapped
            m_page1 = extract_bit(m_addr1, addr_page_low, addr_page_bit)
            # print("first migrate %x %x" % (p_addr1, m_addr1))
            if p_addr1 != m_addr1:
                # print("migration start", self.flatmem.trans_table)
                self.slow_swap_swap_cnt += 1
                self.gen_swap_event(p_addr1, m_addr1)
                # print(self.flatmem.trans_table)
                # print("remove %d %d" % (p_page1, m_page1))
                self.flatmem.trans_table_set(p_page1, p_page1)
                self.flatmem.trans_table_set(m_page1, m_page1)
            # print("swap %x %x" % (m_addr1, p_addr2))
            self.slow_swap_swap_cnt += 1
            self.gen_swap_event(m_addr1, p_addr2)
            self.flatmem.trans_table_set(p_page2, m_page1)
            self.flatmem.trans_table_set(m_page1, p_page2)
            # print("migration done", self.flatmem.trans_table)
            # print("migration done %x <-> %x <-> %x" % (p_addr1, m_addr1, p_addr2))
            # assert(len(self.flatmem.trans_table) <= 2 * self.config["fast_block"]) # in slow swap, size of swapped table is always 2*fast_block*set_num
            assert(len(self.flatmem.trans_table) % 2 == 0) # in slow swap, all swapping is 2-node circle
        elif swap_policy == SwapPolicy.SmartSwap:
            set_id = extract_bit(p_addr1, addr_set_low, addr_set_bit) # p_addr1, p_addr2 must be in the same set
            iteration_cnt = 0
            swap_history = [] # stop replicate swappings
            while True:
                if iteration_cnt > 10: # for debugging
                    print("iteration more than 10")
                    break
                hotness_rank_list = self.metasets[set_id].get_hotness_rank()
                swap_agent = SmartSwap(hotness_rank_list, self.flatmem, set_id)
                (repl_util, repl_src, repl_dst) = swap_agent.get_repl_util()
                (restore_util, restore_src, restore_dst) = swap_agent.find_best_restore_choice()
                if max(repl_util, restore_util) <= 0:
                    break # no more iterations, break the loop
                # print("hotness rank:", hotness_rank_list)
                # print("repl: %d %d %d" % (repl_util, repl_src, repl_dst))
                # print("restore: %d %d %d" % (restore_util, restore_src, restore_dst))
                if repl_util > restore_util:
                    (swap_region1, swap_region2) = (repl_src, repl_dst)
                    self.smart_swap_repl_cnt += 1
                else:
                    (swap_region1, swap_region2) = (restore_src, restore_dst)
                    self.smart_swap_restore_cnt += 1
                swap_paddr1 = make_address(set_id, swap_region1, 0x0)
                swap_paddr2 = make_address(set_id, swap_region2, 0x0)
                swap_page1 = extract_bit(swap_paddr1, addr_page_low, addr_page_bit)
                swap_page2 = extract_bit(swap_paddr2, addr_page_low, addr_page_bit)
                if swap_history.count((swap_paddr1, swap_paddr2)) > 0:
                    break # replicate swappings, break the loop

                m_addr1 = self.metasets[set_id].access_trans_cache(swap_paddr1)
                m_addr2 = self.metasets[set_id].access_trans_cache(swap_paddr2)
                m_page1 = extract_bit(m_addr1, addr_page_low, addr_page_bit)
                m_page2 = extract_bit(m_addr2, addr_page_low, addr_page_bit)
                # print("migration start", self.flatmem.trans_table)
                swap_history.append((swap_paddr1, swap_paddr2))
                self.gen_swap_event(swap_paddr1, swap_paddr2)
                iteration_cnt += 1
                self.flatmem.trans_table_set(swap_page1, m_page2)
                self.flatmem.trans_table_set(swap_page2, m_page1)
                # print("migration done %x(%x) <-> %x(%x)" % (p_addr1, self.flatmem.trans_table[p_page1], p_addr2, self.flatmem.trans_table[p_page2]))
                # print("migration done", self.flatmem.trans_table)
        elif swap_policy == SwapPolicy.NoSwap:
            pass

        self.sync_cycle()
        # print("fast cycle:%d slow cycle:%d flat cycle:%d" % (self.flatmem.fastmem.avail_cycle, self.flatmem.slowmem.avail_cycle, self.avail_cycle))

    def post_access(self, event):
        # migration
        set_id = extract_bit(event.p_addr, addr_set_low, addr_set_bit)
        if self.trig_monitor(event):
            victim_p_region = self.metasets[set_id].find_victim(event)
            if victim_p_region != -1:
                p_address = event.p_addr
                victim_p_address = make_address(set_id, victim_p_region, 0)
                self.start_migration(victim_p_address, p_address, self.config["swap_policy"])

    def access(self, event):
        self.access_cnt += 1
        set_id = extract_bit(event.p_addr, addr_set_low, addr_set_bit)
        if not set_id in self.metasets:
            self.metasets[set_id] = MetaCache(set_id, self.flatmem)
        self.metasets[set_id].track_hotness(event, self.config["repl_policy"])
        self.metasets[set_id].access_trans_cache(event.p_addr)
        # print("cnt: %d granted access %x" % (self.access_cnt, event.p_addr))

        self.flatmem.request(event)

        self.sync_cycle()
        # print("fast cycle:%d slow cycle:%d flat cycle:%d" % (self.flatmem.fastmem.avail_cycle, self.flatmem.slowmem.avail_cycle, self.avail_cycle))
        self.post_access(event)

    def showstats(self):
        print("display all statistics")
        if self.config["swap_policy"] == SwapPolicy.SmartSwap:
            print("smartswap count repl:%d restore:%d" % (self.smart_swap_repl_cnt, self.smart_swap_restore_cnt))
        elif self.config["swap_policy"] == SwapPolicy.FastSwap:
            print("fastswap count %d" % (self.fast_swap_swap_cnt))
        elif self.config["swap_policy"] == SwapPolicy.SlowSwap:
            print("slowswap count %d" % (self.slow_swap_swap_cnt))
        if self.config["bypass_policy"] == BypassPolicy.Probability:
            print("bypass probability: %.2f" % (self.config["bypass_probability"]))
        print("fast cycle:%d slow cycle:%d flat cycle:%d" % (self.flatmem.fastmem.used_cycle, self.flatmem.slowmem.used_cycle, self.avail_cycle))
        print("cached fast trans:%d uncached fast trans:%d rate:%.2f" % (self.flatmem.cached_fast_trans_num, self.flatmem.uncached_fast_trans_num, (self.flatmem.cached_fast_trans_num / (self.flatmem.cached_fast_trans_num + self.flatmem.uncached_fast_trans_num))))
        print("fast access:%d slow access:%d hitrate:%.2f" % (self.flatmem.fastmem.access_cnt, self.flatmem.slowmem.access_cnt, 1.0 * self.flatmem.fastmem.access_cnt / (self.flatmem.fastmem.access_cnt + self.flatmem.slowmem.access_cnt)))




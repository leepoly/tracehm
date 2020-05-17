from enum import Enum

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
    def __init__(self, capacity, read_lat, write_lat):
        self.capacity = capacity
        self.read_lat = read_lat
        self.write_lat = write_lat
    def request(self, event):
        # print("addr:%x capacity:%x" % (event.m_addr, self.capacity))
        if event.m_addr > self.capacity:
            return -1 # out of memory exception
        if event.is_write:
            self.avail_cycle = max(self.avail_cycle, event.current_cycle) + self.write_lat
        else:
            self.avail_cycle = max(self.avail_cycle, event.current_cycle) + self.read_lat
        event.current_cycle = self.avail_cycle

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

    def trans_table_remove(self, page):
        if page in self.trans_table:
            del self.trans_table[page]

    def __init__(self, flatconfig):
        self.fastmem = Memory(flatconfig["fast_cap"], flatconfig["fast_read_lat"], flatconfig["fast_write_lat"])
        self.slowmem = Memory(flatconfig["slow_cap"], flatconfig["slow_read_lat"], flatconfig["slow_write_lat"])
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
        else:
            self.slowmem.avail_cycle = max(self.slowmem.avail_cycle, self.avail_cycle) + cycle
        self.avail_cycle = max(self.fastmem.avail_cycle, self.slowmem.avail_cycle)

    def trans_table_set(self, new_ppage, new_mpage):
        if (new_ppage == new_mpage) and (new_ppage in self.trans_table):
            del self.trans_table[new_ppage]
            return
        self.trans_table[new_ppage] = new_mpage

    def request(self, event):
        event.m_addr = self.translate_address(event.p_addr)
        in_fast = self.maddr_in_fastmem(event.m_addr)
        if not event.is_migration:
            print("granted access %x -> %x in_fast %x" % (event.p_addr, event.m_addr, in_fast))
        if in_fast:
            self.fastmem.request(event)
        else:
            self.slowmem.request(event)

# MetaCaches are in the unit of set. They are usually put in SRAM.
# They store the cache of trans_table for better performance. They also monitor hotness of blocks (by their region id of paddr, not maddr).
# They are used by FlatController to emit advanced operation (swap, duplicate, ...)
class CacheEntry(object):
    hotness = 0
    def __init__(self, hotness):
        self.hotness = hotness

class MetaCache(TimingObj):
    set_id = 0
    timestamp = 0
    def __init__(self, set_id, flatmem):
        self.set_id = set_id
        self.flatmem = flatmem

    entries = {} # region_id -> hotness
    cached_trans_table = [] # List of pages. we do not actually duplicate transtable. Use a bool array to cancel latency for cached mapping.

    def trans_cache_remove(self, page):
        if self.cached_trans_table.count(page):
            self.cached_trans_table.remove(page)

    def track_hotness(self, event):
        self.timestamp += 1
        p_region = extract_bit(event.p_addr, addr_region_low, addr_region_bit)
        self.entries[p_region] = CacheEntry(self.timestamp) # we use timestamp LRU to track hotness

    def access_trans_cache(self, p_addr):
        p_page = extract_bit(p_addr, addr_page_low, addr_page_bit)
        # print(self.cached_trans_table)
        if not p_page in self.cached_trans_table:
            # print("trans_table cache miss add 1 cycle")
            self.flatmem.advance_cycle(True, self.flatmem.trans_table_read_lat) # if miss, add translation latency
            self.flatmem.sync_cycle()
            self.cached_trans_table.append(p_page)
            if len(self.cached_trans_table) > c_trans_cache_capacity_per_set:
                self.cached_trans_table.pop(0) # LRU replacement is used. pop the first element
        # if hit, no latency added
        else:
            self.cached_trans_table.remove(p_page)
            self.cached_trans_table.append(p_page)
        return self.flatmem.translate_address(p_addr)

    def find_victim(self, event):
        min_hotness = INF
        min_hotness_region = -1
        for region_id, item in self.entries.items():
            p_addr = make_address(self.set_id, region_id, 0)
            if self.flatmem.paddr_in_fastmem(p_addr):
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
    "fast_cap": 0x12000, # 8KB, 2 blocks
    "slow_cap": 0x16000, # 16KB, 4 blocks
    "fast_read_lat": 1,
    "fast_write_lat": 1,
    "slow_read_lat": 2,
    "slow_write_lat": 2,
    "fast_block": 2,
    "swap_policy": SwapPolicy.FastSwap,
}

class SmartSwap(object):
    swap_alpha = 3.5 # benefit of relative rank
    swap_beta = 6 # cost of one migration
    swap_gamma = 1.0 # benefit of one empty slot
    slow_mru_region = -1
    fast_region = [] # head is the LRU while tail is the MRU
    def __init__(self, rank_list, flatmem):
        self.rank_list = rank_list # head is the LRU while tail is the MRU
        self.flatmem = flatmem
        for ppage in self.rank_list:
            is_fast = self.flatmem.ppage_in_fastmem(ppage)
            if (not is_fast) and (self.slow_mru_region == -1):
                self.slow_mru_region = ppage
            elif (is_fast):
                self.fast_region.append(ppage)

    def search_page_in_rank(self, page):
        for i in range(len(self.rank_list)):
            if self.rank_list[i] == page:
                return i # return the rank
        return -1

    def find_best_restore_choice(self):
        max_util = -1
        best_src = best_dst = -1
        for ppage in self.fast_region:
            ppage_prev = self.flatmem.translate_page_inv(ppage)
            ppage_rank = self.search_page_in_rank(ppage)
            ppage_prev_rank = self.flatmem.translate_page_inv(ppage_rank)

            if (self.swap_alpha * (ppage_prev_rank - ppage_rank) + self.swap_gamma - self.swap_beta) > max_util:
                max_util = self.swap_alpha * (ppage_prev_rank - ppage_rank) + self.swap_gamma - self.swap_beta
                best_src, best_dst = ppage, ppage_prev
        return (max_util, best_src, best_dst)

    def get_repl_util(self):
        # swap most inactive fast and most active slowblock
        # we use their LRU order as their rank order
        slow_rank = self.search_page_in_rank(self.slow_mru_region)
        fast_rank = self.search_page_in_rank(self.fast_region[0])
        repl_util = self.swap_alpha * (slow_rank - fast_rank) - self.swap_beta
        # print("repl get: %d %d %d" % (slow_rank, fast_rank, repl_util))
        # print("fast rank: ", self.fast_page)
        return (repl_util, self.slow_mru_region, self.fast_region[0])

class FlatController(TimingObj):
    metasets = {} # set_id -> MetaCache
    config = flat_config1
    flatmem = FlatMemory(config)

    def trig_monitor(self, event):
        in_fast = self.flatmem.paddr_in_fastmem(event.p_addr)
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
            m_addr1 = self.metasets[set_id].access_trans_cache(p_addr1)
            m_addr2 = self.metasets[set_id].access_trans_cache(p_addr2)
            m_page1 = extract_bit(m_addr1, addr_page_low, addr_page_bit)
            m_page2 = extract_bit(m_addr2, addr_page_low, addr_page_bit)
            # print("p1 %x m1 %x  p2 %x m2 %x" % (p_addr1, m_addr1, p_addr2, m_addr2))
            self.flatmem.trans_table_set(p_page1, m_page2)
            self.flatmem.trans_table_set(p_page2, m_page1)
            print("migration done", self.flatmem.trans_table)
            # print("migration done %x(%x) <-> %x(%x)" % (p_addr1, self.flatmem.trans_table[p_page1], p_addr2, self.flatmem.trans_table[p_page2]))
        elif swap_policy == SwapPolicy.SlowSwap:
            m_addr1 = self.metasets[set_id].access_trans_cache(p_addr1) # check whether fastblock is not swapped
            m_page1 = extract_bit(m_addr1, addr_page_low, addr_page_bit)
            if p_addr1 != m_addr1:
                self.gen_swap_event(p_addr1, m_addr1)
                # print(self.flatmem.trans_table)
                # print("remove %d %d" % (p_page1, m_page1))
                self.flatmem.trans_table_remove(p_page1)
                self.flatmem.trans_table_remove(m_page1)
            self.gen_swap_event(m_addr1, p_addr2)
            self.flatmem.trans_table_set(p_page2, m_page1)
            self.flatmem.trans_table_set(m_page1, p_page2)
            print("migration done %x <-> %x <-> %x" % (p_addr1, m_addr1, p_addr2))
            assert(len(self.flatmem.trans_table) <= 2 * self.config["fast_block"]) # in slow swap, size of swapped table is always 2*fast_block
        elif swap_policy == SwapPolicy.SmartSwap:
            set_id = extract_bit(p_addr1, addr_set_low, addr_set_bit) # p_addr1, p_addr2 must be in the same set
            hotness_rank_list = self.metasets[set_id].get_hotness_rank()
            print("hotness rank:", hotness_rank_list)
            swap_agent = SmartSwap(hotness_rank_list, self.flatmem)
            (repl_util, repl_src, repl_dst) = swap_agent.get_repl_util()
            print("repl: %d %d %d" % (repl_util, repl_src, repl_dst))
            (restore_util, restore_src, restore_dst) = swap_agent.find_best_restore_choice()
            print("restore: %d %d %d" % (restore_util, restore_src, restore_dst))
            if max(repl_util, restore_util) > 0:
                if repl_util > restore_util:
                    (swap_region1, swap_region2) = (repl_src, repl_dst)
                else:
                    (swap_region1, swap_region2) = (restore_src, restore_dst)
                swap_paddr1 = make_address(set_id, swap_region1, 0x0)
                swap_paddr2 = make_address(set_id, swap_region2, 0x0)
                swap_page1 = extract_bit(swap_paddr1, addr_page_low, addr_page_bit)
                swap_page2 = extract_bit(swap_paddr2, addr_page_low, addr_page_bit)
                print("DEBUG set %d region %d page %x" % (set_id, swap_region1, swap_page1))

                m_addr1 = self.metasets[set_id].access_trans_cache(swap_paddr1)
                m_addr2 = self.metasets[set_id].access_trans_cache(swap_paddr2)
                m_page1 = extract_bit(m_addr1, addr_page_low, addr_page_bit)
                m_page2 = extract_bit(m_addr2, addr_page_low, addr_page_bit)
                print("migration start", self.flatmem.trans_table)
                print("DEBUG ppage1 %d  ppage2 %d" % (swap_page1, swap_page2))
                print("DEBUG mpage1 %d  mpage2 %d" % (m_page2, m_page1))
                self.gen_swap_event(swap_paddr1, swap_paddr2)
                self.flatmem.trans_table_set(swap_page1, m_page2)
                self.flatmem.trans_table_set(swap_page2, m_page1)
                # print("migration done %x(%x) <-> %x(%x)" % (p_addr1, self.flatmem.trans_table[p_page1], p_addr2, self.flatmem.trans_table[p_page2]))
                print("migration done", self.flatmem.trans_table)
                # TODO: for loop

        self.sync_cycle()
        print("fast cycle:%d slow cycle:%d flat cycle:%d" % (self.flatmem.fastmem.avail_cycle, self.flatmem.slowmem.avail_cycle, self.avail_cycle))

    def post_access(self, event):
        # migration
        set_id = extract_bit(event.p_addr, addr_set_low, addr_set_bit)
        # print("DEBUG 1 %x" % self.trig_monitor(event))
        if self.trig_monitor(event):
            victim_p_region = self.metasets[set_id].find_victim(event)
            if victim_p_region != -1:
                p_address = event.p_addr
                victim_p_address = make_address(set_id, victim_p_region, 0)
                self.start_migration(victim_p_address, p_address, self.config["swap_policy"])

    def access(self, event):
        set_id = extract_bit(event.p_addr, addr_set_low, addr_set_bit)
        if not set_id in self.metasets:
            self.metasets[set_id] = MetaCache(set_id, self.flatmem)
        self.metasets[set_id].track_hotness(event)
        self.metasets[set_id].access_trans_cache(event.p_addr)
        # print("granted access %x" % event.p_addr)

        self.flatmem.request(event)

        self.sync_cycle()
        print("fast cycle:%d slow cycle:%d flat cycle:%d" % (self.flatmem.fastmem.avail_cycle, self.flatmem.slowmem.avail_cycle, self.avail_cycle))
        self.post_access(event)



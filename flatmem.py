class TimingObj(object):
    avail_cycle = 0

class MemEvent(object):
    def __init__(self, address, is_write, current_cycle):
        self.address = address
        self.is_write = is_write
        self.current_cycle = current_cycle


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

class Memory(TimingObj):
    def __init__(self, capacity, read_lat, write_lat):
        self.capacity = capacity
        self.read_lat = read_lat
        self.write_lat = write_lat
    def request(self, event):
        if event.address > self.capacity:
            return -1 # out of memory exception
        if event.is_write:
            self.avail_cycle = max(self.avail_cycle, event.current_cycle) + self.write_lat
        else:
            self.avail_cycle = max(self.avail_cycle, event.current_cycle) + self.read_lat
        event.current_cycle = self.avail_cycle

def extract_bit(value, start, len):
    value = value >> start
    mask = 1<<len - 1
    return value & mask

def make_address(addr_set, addr_region, addr_offset):
    address = addr_set
    address = address << addr_region_bit | addr_region
    address = address << addr_offset_bit | addr_offset
    return address

class FlatMemory(TimingObj):
    trans_table = {} # in fastmem. p_page -> m_page

    def __init__(self, flatconfig):
        self.fastmem = Memory(flatconfig.fast_cap, flatconfig.fast_read_lat, flatconfig.fast_write_lat)
        self.slowmem = Memory(flatconfig.slow_cap, flatconfig.slow_read_lat, flatconfig.slow_write_lat)
        self.trans_table_read_lat = flatconfig.fast_read_lat
        self.fast_block = flatconfig.fast_block

    def mpage_in_fastmem(self, maddress):
        region = extract_bit(maddress, addr_region_low-addr_page_low, addr_region_bit)
        return region < self.fast_block

    def maddr_in_fastmem(self, maddress):
        region = extract_bit(maddress, addr_region_low, addr_region_bit)
        return region < self.fast_block

    def paddr_in_fastmem(self, paddress):
        p_page = extract_bit(paddress, addr_page_low, addr_page_bit)
        m_page = self.trans_table.get(p_page, default=p_page)
        return self.mpage_in_fastmem(m_page)

    def translate_address(self, paddress):
        p_page = extract_bit(paddress, addr_page_low, addr_page_bit)
        p_offset = extract_bit(paddress, addr_offset_low, addr_offset_bit)
        m_page = self.trans_table.get(p_page, default=p_page)
        m_address = m_page << addr_page_low | p_offset
        return m_address

    def sync_cycle(self):
        self.avail_cycle = max(self.fastmem.avail_cycle, self.slowmem.avail_cycle)

    def advance_cycle(self, is_fastmem, cycle):
        if is_fastmem:
            self.fastmem.avail_cycle = max(self.fastmem.avail_cycle, self.avail_cycle) + cycle
        else:
            self.slowmem.avail_cycle = max(self.slowmem.avail_cycle, self.avail_cycle) + cycle
        self.avail_cycle = max(self.fastmem.avail_cycle, self.slowmem.avail_cycle)

    def request(self, event):
        event.address = self.translate_address(event.address)
        in_fast = self.maddr_in_fastmem(event.address)
        self.advance_cycle(True, self.trans_table_read_lat) # advance fastmem avail_cycle. trans_table always in fastmem
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
    def __init__(self, set_id):
        self.set_id = set_id

    entries = {} # region_id -> hotness
    def track_hotness(self, event):
        self.timestamp += 1
        p_region = extract_bit(event.address, addr_region_low, addr_region_bit)
        self.entries[p_region] = CacheEntry(self.timestamp) # we use timestamp LRU to track hotness

    def find_victim(self, event):
        min_hotness = INF
        min_hotness_region = -1
        for region_id, item in self.entries:
            if item.hotness < min_hotness:
                min_hotness = item.hotness
                min_hotness_region = region_id
        if min_hotness_region != -1:
            return min_hotness_region
        return -1

flat_config1 = {
    "fast_cap": 1<<13, # 8KB, 2 blocks
    "slow_cap": 1<<14, # 16KB, 4 blocks
    "fast_read_lat": 1,
    "fast_write_lat": 1,
    "slow_read_lat": 2,
    "slow_write_lat": 2,
    "fast_block": 2
}

class FlatController(TimingObj):
    metasets = {} # set_id -> MetaCache
    flatmem = FlatMemory(flat_config1)

    def trig_monitor(self, event):
        return True # always migrate

    def sync_cycle(self):
        self.flatmem.sync_cycle()
        self.avail_cycle = max(self.avail_cycle, self.flatmem.avail_cycle)

    def start_migration(self, p_addr1, p_addr2):
        infast_1 = self.flatmem.paddr_in_fastmem(p_addr1)
        infast_2 = self.flatmem.paddr_in_fastmem(p_addr2)
        assert(infast_1 ^ infast_2) # must be one fastblock and one slowblock
        self.flatmem.request(MemEvent(p_addr1, False, self.avail_cycle))
        self.flatmem.request(MemEvent(p_addr2, False, self.avail_cycle))
        self.flatmem.request(MemEvent(p_addr1, True, self.avail_cycle))
        self.flatmem.request(MemEvent(p_addr2, True, self.avail_cycle))
        self.sync_cycle()

    def post_access(self, event):
        # migration
        set_id = extract_bit(event.address, addr_set_low, addr_set_bit)
        if self.trig_monitor(event):
            victim_p_region = self.metasets[set_id].find_victim(event)
            if victim_p_region != -1:
                p_address = event.address
                victim_p_address = make_address(set_id, victim_p_region, 0)
                self.start_migration(victim_p_address, p_address)

    def access(self, event):
        set_id = extract_bit(event.address, addr_set_low, addr_set_bit)
        if not set_id in self.metasets:
            self.metasets[set_id] = MetaCache(set_id)
        self.metasets[set_id].track_hotness(event)

        self.flatmem.request(event)

        self.sync_cycle()
        self.post_access(event)



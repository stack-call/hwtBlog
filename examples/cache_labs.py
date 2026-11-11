#!/usr/bin/env python3
"""12 small deterministic teaching labs. No Redis/MySQL required.
Run: python3 examples/cache_labs.py all
or:  python3 examples/cache_labs.py 05
These are application-level models, NOT production Redis integration tests.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
import random
import sys
import threading
import time

class DB:
    def __init__(self):
        self.rows = {42: {'id':42,'title':'Old Title','version':1}}
        self.queries = 0
    def read(self, id):
        self.queries += 1
        return dict(self.rows[id]) if id in self.rows else None

class KV:
    def __init__(self): self.data={}
    def get(self,k): return self.data.get(k)
    def set(self,k,v): self.data[k]=v
    def delete(self,k): self.data.pop(k,None)

# JSON wire string to model Redis GET; missing is None.
def encode(row): return json.dumps(row,ensure_ascii=False) if row is not None else None
def decode(value): return json.loads(value) if value is not None else None

def lab01():
    db=DB()
    for _ in range(1000): db.read(42)
    before=db.queries
    db.queries=0; cache={}
    for _ in range(1000):
        if 42 not in cache: cache[42]=db.read(42)
        _=cache[42]
    assert (before,db.queries)==(1000,1)
    return f'without cache DB={before}; with local cache DB={db.queries}'

def lab02():
    db=DB(); shared=KV(); localA={}; localB={}
    def fetch_local(local):
        if 42 not in local: local[42]=db.read(42)
        return local[42]
    fetch_local(localA);fetch_local(localB); local_count=db.queries
    db.queries=0
    def fetch_shared():
        s=shared.get('post:42')
        if s is not None:return decode(s)
        row=db.read(42); shared.set('post:42',encode(row));return row
    a=fetch_shared();b=fetch_shared()
    assert a==b and local_count==2 and db.queries==1
    return f'two local caches DB={local_count}; one shared cache DB={db.queries}'

def lab03():
    db=DB(); shared=KV(); key='post:42'
    def read():
        item=shared.get(key)
        if item is not None:return decode(item)
        item=db.read(42);shared.set(key,encode(item));return item
    old=read()['version']
    db.rows[42]={'id':42,'title':'New Title','version':2}
    stale=read()['version']
    shared.delete(key)
    fresh=read()['version']
    assert (old,stale,fresh)==(1,1,2)
    return f'cached={old}; after DB update without invalidation={stale}; after DEL={fresh}'

def lab04():
    db=DB(); data=KV(); epoch={'post:42':0}; key='post:42'
    # Slow reader captures version=1 and epoch=0, then gets suspended.
    seen_epoch=epoch[key]; old=db.read(42)
    # Writer commits and completes cache invalidation.
    db.rows[42]={'id':42,'title':'New Title','version':2}
    data.delete(key);epoch[key]+=1
    # Without a guard, the stale load resurrects v1.
    data.set(key,encode(old));unprotected=decode(data.get(key))['version']
    data.delete(key)
    # Conditional fill refuses if invalidation epoch has changed.
    accepted=(seen_epoch==epoch[key]);
    if accepted:data.set(key,encode(old))
    fresh=db.read(42);data.set(key,encode(fresh))
    assert unprotected==1 and not accepted and decode(data.get(key))['version']==2
    return f'unprotected stale backfill v={unprotected}; guard accepted={accepted}; final v=2'

class Flight:
    def __init__(self): self.lock=threading.Lock();self.active={}
    def do(self,key,fn):
        with self.lock:
            if key in self.active:
                event,box=self.active[key]; leader=False
            else:
                event,box=threading.Event(),{};self.active[key]=(event,box);leader=True
        if not leader:
            event.wait();return box['value']
        try:box['value']=fn()
        finally:
            with self.lock:self.active.pop(key,None)
            event.set()
        return box['value']

def lab05():
    n=24; db=DB(); start=threading.Barrier(n)
    def plain(_):
        start.wait()
        value=db.read(42)
        time.sleep(.035)
        return value
    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(plain,range(n)))
    raw=db.queries;db.queries=0;start=threading.Barrier(n);flight=Flight()
    def merged(_):
        start.wait()
        return flight.do('post:42',lambda: (time.sleep(.04),db.read(42))[1])
    with ThreadPoolExecutor(max_workers=n) as pool:
        results=list(pool.map(merged,range(n)))
    assert raw==n and db.queries==1 and len(results)==n
    return f'parallel misses without merging DB={raw}; with same-process singleflight DB={db.queries}'

class TTLCache:
    def __init__(self):self.items={}
    def put(self,k,v,now,ttl):self.items[k]=(v,now+ttl)
    def get(self,k,now):
        p=self.items.get(k)
        return p[0] if p is not None and now < p[1] else None
    def raw_count(self):return len(self.items)

def lab06():
    c=TTLCache();c.put('post:42','v1',now=0,ttl=10)
    assert c.get('post:42',9)=='v1'
    assert c.get('post:42',10) is None
    assert c.raw_count()==1 # lazy physical cleanup not implemented
    return 't=9 cache hit; t=10 cache miss; stored physical entries may remain until cleanup'

def lab07():
    db=DB(); cache=TTLCache();sentinel={'not_found':True}
    def get(id,now):
        cached=cache.get(id,now)
        if cached is not None: return None if cached==sentinel else cached
        row=db.read(id)
        cache.put(id,row if row else sentinel,now=now,ttl=2 if row is None else 60)
        return row
    for _ in range(100): assert get(999999,0) is None
    assert db.queries==1
    db.rows[999999]={'id':999999,'title':'Created','version':1}
    assert get(999999,1) is None
    assert get(999999,2)['version']==1
    return '100 repeated nonexistent-ID requests DB=1; newly created row hidden until negative TTL expires'

def lab08():
    count=120;base=60
    aligned=[base]*count
    randomized=[base+(i%30) for i in range(count)] # fixed offset for reproducibility
    def peak(expiry):
        times={}
        for t in expiry:times[t]=times.get(t,0)+1
        return max(times.values())
    assert peak(aligned)==120 and peak(randomized)==4
    return f'120 distinct keys: peak same-second expiry {peak(aligned)} vs staggered {peak(randomized)}'

def lab09():
    lru=OrderedDict();evictions=[]
    def access(key):
        if key in lru: lru.move_to_end(key);return
        if len(lru)==3:evictions.append(lru.popitem(last=False)[0])
        lru[key]=1
    for k in 'ABCAD':access(k)
    assert evictions==['B'] and list(lru)==['C','A','D']
    return f'capacity=3, trace=A B C A D: evicted={evictions[0]}, resident={list(lru)}'

def lab10():
    redis_reads=0; L1={}
    def redis_get():
        nonlocal redis_reads
        redis_reads+=1;return {'id':42,'title':'Old Title','version':1}
    for _ in range(1000): redis_get()
    no_l1=redis_reads;redis_reads=0
    for _ in range(1000):
        if 42 not in L1:L1[42]=redis_get()
    assert no_l1==1000 and redis_reads==1
    return f'1000 hot-key reads Redis QPS count: without L1={no_l1}, with L1={redis_reads}'

def lab11():
    L1_A={42:{'version':1}};L1_B={42:{'version':1}}
    db_version=2
    L1_A.pop(42,None) # same instance local invalidation
    stale=L1_B[42]['version']
    # pubsub message reaches B
    L1_B.pop(42,None)
    fresh={'version':db_version};L1_B[42]=fresh
    assert stale==1 and L1_B[42]['version']==2
    # missed-message counterexample
    L1_B[42]={'version':2};new_db_version=3
    missed=L1_B[42]['version']
    assert new_db_version==3 and missed==2
    return f'without event B=v{stale}; after event/reload B=v2; missed event leaves B=v{missed} while DB=v3'

def lab12():
    n=30; limit=3; sem=threading.BoundedSemaphore(limit)
    barrier=threading.Barrier(n);mutex=threading.Lock();active=0;peak=0
    def one(_):
        nonlocal active,peak
        barrier.wait()
        if not sem.acquire(blocking=False):return '503 DEGRADED'
        try:
            with mutex:
                active+=1;peak=max(peak,active)
            time.sleep(.07) # DB query while Redis is down
            return '200 DB FALLBACK'
        finally:
            with mutex:active-=1
            sem.release()
    with ThreadPoolExecutor(max_workers=n) as pool: results=list(pool.map(one,range(n)))
    successes=results.count('200 DB FALLBACK'); failures=results.count('503 DEGRADED')
    assert peak <= limit and successes+failures==n and failures>0
    return f'30 requests during Redis outage: DB peak concurrency={peak}; fallback={successes}, degraded={failures}'

LABS={f'{i:02d}':globals()[f'lab{i:02d}'] for i in range(1,13)}
if __name__=='__main__':
    tasks=list(LABS) if len(sys.argv)==1 or sys.argv[1]=='all' else sys.argv[1:]
    for chapter in tasks:
        try: print(f'{chapter}: {LABS[chapter]()}')
        except KeyError:raise SystemExit(f'Unknown chapter {chapter}; choose 01..12 or all')

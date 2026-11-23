// @vitest-environment jsdom
import {afterEach, beforeAll, expect, it, vi} from 'vitest';
import {cleanup, render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import Overview from './Overview';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});

it('keeps the former cockpit summary without its own heading or pace warning',async()=>{
 const overview={last_observed:'2026-10-22T03:00:00Z',counts:{posts:10,accounts:4,counter_snapshots:30,news_articles:2,news_full_bodies:1},quality:{posts_without_counters:2,unknown_accounts:1},score:{official_total:'4369',activity:{unparsed:0}},traffic:{current:{}},
  threshold:{total:'4369',observed_at:'2026-10-22T02:50:00Z',snapshot_age_minutes:10,stale:false,rate_basis:'12h',rates:{'6h':{points_per_hour:29},'12h':{points_per_hour:58}},polls:[{poll:2,threshold:7000,estimated_count_at:'2026-10-24T20:11:00Z',hours_left:41.5,gap:'2631',required_points_per_hour_now:40.2}]}};
 vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL)=>new Response(JSON.stringify(String(input).startsWith('/api/overview')?overview:{items:[],total:0,limit:5,offset:0}),{headers:{'Content-Type':'application/json'}})));
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><MemoryRouter><Overview/></MemoryRouter></QueryClientProvider>);
 expect(await screen.findByText('官方总分')).toBeTruthy();
 expect(screen.getByText('已采集帖子')).toBeTruthy();
 expect(screen.getByText('数据覆盖')).toBeTruthy();
 expect(screen.getByRole('link',{name:'1 个账号'}).getAttribute('href')).toBe('/accounts');
 // The home page shows the pace warning above both tabs, and the shell gives the page its heading.
 expect(screen.queryByRole('alert')).toBeNull();
 expect(screen.queryAllByRole('heading',{level:1})).toHaveLength(0);
});

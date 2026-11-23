// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {MemoryRouter, useLocation} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import AccountClusters, {orderClusters} from './AccountClusters';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient|undefined;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});
function Location() {const location=useLocation();return <output data-testid="location">{location.pathname}{location.search}</output>;}

const clusters=[
 {cluster_id:'c2',size:4,engagement_per_hour:20,posts_per_hour:2,bot_share:0.1,stance_counts:{yes:1,no:3,undecided:0,unrelated:0},blue_candidates:0,members:[],top_posts:[],reasons:[]},
 {cluster_id:'own',is_own:true,size:5,engagement_per_hour:30,posts_per_hour:3,bot_share:0,stance_counts:{yes:9,no:1,undecided:0,unrelated:0},members:[{author_id:5554,username:'maya'}],top_posts:[{post_id:7,excerpt:'Our post',points:40}]},
 {cluster_id:'c1',size:10,engagement_per_hour:90,posts_per_hour:12,bot_share:0.8,stance_counts:{yes:0,no:8,undecided:2,unrelated:0},blue_candidates:3,members_total:10,
  members:[{author_id:55,username:'spammy',is_bot:true,bot_reasons:['behavior']},{author_id:56,username:'echo',is_bot:false,bot_reasons:['duplicate_text']}],
  top_posts:[{post_id:901,excerpt:'Vote now',points:15}],reasons:[{rule:'behaviour_bot_rule',count:8},{rule:'shared_text_post',count:5}]}
];

function setup(initial='/analysis/clusters') {
 vi.stubGlobal('fetch',vi.fn(async()=>new Response(JSON.stringify({generated_at:'2026-10-22T01:00:00Z',window_hours:72,stale:false,caveats:['簇每 10 分钟重算一次'],clusters}),{headers:{'Content-Type':'application/json'}})));
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[initial]}><AccountClusters/><Location/></MemoryRouter></QueryClientProvider>);
}
const rowNames=()=>screen.getAllByRole('row').slice(1).map(row=>row.querySelector('td:nth-child(2)')?.textContent||'').filter(Boolean);

describe('account clusters',()=>{
 it('warns that clusters are behaviour groups, not confirmed teams',async()=>{
  setup();
  expect(await screen.findByText('簇是按行为聚出来的账号组，不等于已确认的队伍')).toBeTruthy();
 });

 it('pins our accounts first and orders the rest by engagement per hour',async()=>{
  setup();
  await screen.findByText('我们');
  expect(rowNames()).toEqual(['我们对照','簇 c1','簇 c2']);
 });

 it('keeps our row first when sorting by size',async()=>{
  setup('/analysis/clusters?sort=size');
  await screen.findByText('我们');
  expect(rowNames()).toEqual(['我们对照','簇 c1','簇 c2']);
  expect(orderClusters(clusters,'bots').map(row=>row.cluster_id)).toEqual(['own','c1','c2']);
 });

 it('opens a cluster from the address with members, posts and bot reasons, and links to its report candidates',async()=>{
  setup('/analysis/clusters?cluster=c1');
  expect(await screen.findByText('成员账号 · 10')).toBeTruthy();
  expect(screen.getByText('疑似机器人')).toBeTruthy();
  expect(screen.getByText('疑似重复文本')).toBeTruthy();
  expect(screen.getByText('行为判定为机器人')).toBeTruthy();
  expect(screen.getByRole('link',{name:'#901'}).getAttribute('href')).toBe('/posts?post=901');
  expect(screen.getByRole('link',{name:'查看这一簇的 3 条举报候选'}).getAttribute('href')).toBe('/blue?cluster=c1');
 });

 it('records the opened cluster in the address so it can be shared',async()=>{
  setup();
  await screen.findByText('我们');
  const row=screen.getByText('簇 c2').closest('tr') as HTMLElement;
  fireEvent.click(within(row).getByRole('button'));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toBe('/analysis/clusters?cluster=c2'));
 });
});

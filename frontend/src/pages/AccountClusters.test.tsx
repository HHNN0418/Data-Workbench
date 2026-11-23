// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {MemoryRouter, useLocation} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import AccountClusters, {blueLink, clusterForBlue, orderClusters} from './AccountClusters';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient|undefined;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});
function Location() {const location=useLocation();return <output data-testid="location">{location.pathname}{location.search}</output>;}

// Field names from docs/handoff/C-api.md §2.
const base={members_total:0,members:[],hourly_writes:Array(48).fill(0),kinds:{post:0,reply:0,repost:0},post_share:null,reply_share:null,post_reply_ratio:null,replies_to_citizens_share:null,
 stance:{yes:null,no:null,undecided:null},stance_posts:0,trending_share:null,citizen_replies:0,citizen_reposts:0,bot_flagged:0,bot_share:null,bot_reasons:{},blue_candidates:null,blue_cluster_ids:[],examples:[]};
const own={...base,cluster_id:'own',is_own:true,label:'我们自己（Agent Zero）',accounts:5,members_total:5,writes:96,citizen_points_per_hour:4.4,bot_share:0,
 members:[{account_id:5554,username:'maya',is_bot:false,bot_reasons:[],writes:40}],kinds:{post:30,reply:60,repost:6},post_share:0.312,post_reply_ratio:0.5,
 stance:{yes:0.9,no:0.1,undecided:0},stance_posts:10,examples:[{post_id:7,why:'citizen_replies',excerpt:'Our post'}]};
const big={...base,cluster_id:'c5521',accounts:10,members_total:10,writes:480,citizen_points_per_hour:9.5,bot_flagged:8,bot_share:0.8,bot_reasons:{duplicate_text:5,behavior:8},
 kinds:{post:100,reply:300,repost:80},post_share:0.208,replies_to_citizens_share:0.25,stance:{yes:0,no:0.8,undecided:0.2},stance_posts:40,trending_share:0.05,citizen_replies:30,citizen_reposts:2,
 members:[{account_id:55,username:'spammy',is_bot:true,bot_reasons:['behavior','fast_reply'],writes:200},{account_id:56,username:null,is_bot:false,bot_reasons:['duplicate_text'],writes:20}],
 blue_candidates:3,blue_cluster_ids:['c55','a56'],examples:[{post_id:901,why:'shared_text',excerpt:'Vote now, vote now'}]};
const busy={...base,cluster_id:'c77',accounts:4,members_total:4,writes:960,citizen_points_per_hour:2,bot_flagged:1,bot_share:0.25,bot_reasons:{always_on:1},blue_candidates:0,
 members:[{account_id:77,username:'night',is_bot:true,bot_reasons:['always_on'],writes:900}]};
const payload={generated_at:'2026-10-22T01:00:00Z',window:{hours:48,from:'2026-10-20T01:00:00Z',to:'2026-10-22T01:00:00Z'},note:'簇是按行为聚出来的账号组，不等于已确认的队伍',
 own,clusters:[big,busy],clusters_total:12,clustered_accounts:140,citizen_accounts:900,stale:false,
 method:{link:['同一段文字被 ≥3 个账号发布'],not_used:['30 分钟活跃度相关','创建时间相近'],min_accounts:3,citizen_points:'回复 ×15 + 转发 ×10'}};

function setup(initial='/analysis/clusters',data:unknown=payload) {
 const fetch=vi.fn(async(_input:RequestInfo|URL,_options?:RequestInit)=>new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}}));
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[initial]}><AccountClusters/><Location/></MemoryRouter></QueryClientProvider>);
 return fetch;
}
const rowNames=()=>Array.from(document.querySelectorAll('.ant-table-tbody tr[data-row-key]')).map(row=>row.querySelector('.cluster-name strong')?.textContent);
// The header cell, not the hidden copy antd keeps for column measuring.
const header=(name:string)=>screen.getAllByRole('columnheader').find(cell=>cell.textContent===name) as HTMLElement;
const row=(name:string)=>screen.getByText(name,{selector:'.cluster-name strong'}).closest('tr') as HTMLElement;

describe('account clusters',()=>{
 it('says on top that clusters are behaviour groups, not confirmed teams, and asks without a window',async()=>{
  const fetch=setup();
  expect(await screen.findByText('簇是按行为聚出来的账号组，不等于已确认的队伍')).toBeTruthy();
  expect(fetch.mock.calls.map(([input])=>String(input))).toEqual(['/api/clusters']);
  expect(await screen.findByText('显示 2 / 12 个簇 · 簇内 140 个账号 · 校准市民 900 个')).toBeTruthy();
 });

 it('pins our own row first and orders the rest by citizen points per hour',async()=>{
  setup();
  await screen.findByText('我们自己（Agent Zero）');
  expect(rowNames()).toEqual(['我们自己（Agent Zero）','簇 c5521','簇 c77']);
  expect(row('我们自己（Agent Zero）').className).toContain('cluster-own');
  const cells=Array.from(row('簇 c77').querySelectorAll('td')).map(cell=>cell.textContent);
  // accounts, citizen points per hour, writes per hour (960 / 48), post share …
  expect(cells.slice(2,5)).toEqual(['4','2','20']);
  expect(within(row('簇 c5521')).getByText('Yes 0% · No 80% · 犹豫 20%')).toBeTruthy();
 });

 it('sorts from the column headers and keeps our row first',async()=>{
  setup('/analysis/clusters?sort=writes');
  await screen.findByText('我们自己（Agent Zero）');
  expect(rowNames()).toEqual(['我们自己（Agent Zero）','簇 c77','簇 c5521']);
  fireEvent.click(header('账号数'));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toBe('/analysis/clusters?sort=accounts'));
  expect(rowNames()).toEqual(['我们自己（Agent Zero）','簇 c5521','簇 c77']);
  fireEvent.click(header('账号数'));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toBe('/analysis/clusters?sort=accounts&order=asc'));
  expect(rowNames()).toEqual(['我们自己（Agent Zero）','簇 c77','簇 c5521']);
 });

 it('opens a cluster with its members, sample posts and bot reasons, and links to its Blue candidates',async()=>{
  setup('/analysis/clusters?cluster=c5521');
  expect(await screen.findByText('成员账号 · 10')).toBeTruthy();
  expect(screen.getByRole('link',{name:'@spammy'}).getAttribute('href')).toBe('/accounts?q=55');
  expect(screen.getByRole('link',{name:'@56'}).getAttribute('href')).toBe('/accounts?q=56');
  expect(screen.getByText('行为规则命中、回复非常快')).toBeTruthy();
  expect(screen.getByText('疑似重复文本')).toBeTruthy();
  expect(screen.getByText('机器人判定原因 · 8 个账号')).toBeTruthy();
  const reasons=screen.getByText('机器人判定原因 · 8 个账号').closest('section') as HTMLElement;
  expect(within(reasons).getAllByRole('listitem').map(item=>item.textContent)).toEqual(['行为规则命中 · 8 人','和其他账号文本相同 · 5 人']);
  expect(screen.getByRole('link',{name:'#901'}).getAttribute('href')).toBe('/posts?post=901');
  expect(screen.getByText('同文')).toBeTruthy();
  expect(screen.getByText('Vote now, vote now')).toBeTruthy();
  expect(screen.getByRole('link',{name:'在 Blue 候选里查看这一簇的 3 个账号'}).getAttribute('href')).toBe('/blue?clusters=c55,a56');
  expect(screen.getByRole('link',{name:'3'}).getAttribute('href')).toBe('/blue?clusters=c55,a56');
 });

 it('opens the cluster holding a Blue candidate when linked from the Blue page',async()=>{
  setup('/analysis/clusters?blue_cluster=a56');
  expect(await screen.findByText('成员账号 · 10')).toBeTruthy();
  cleanup();client?.clear();
  setup('/analysis/clusters?blue_cluster=c999');
  expect(await screen.findByText('Blue 的簇 c999 不在当前的账号簇里')).toBeTruthy();
  expect(screen.queryByText(/^成员账号/)).toBeNull();
 });

 it('records the opened cluster in the address so it can be shared',async()=>{
  setup('/analysis/clusters?blue_cluster=a56');
  await screen.findByText('成员账号 · 10');
  fireEvent.click(within(row('簇 c77')).getByRole('button'));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toBe('/analysis/clusters?cluster=c77'));
  expect(await screen.findByText('成员账号 · 4')).toBeTruthy();
 });

 it('marks a result that is being recomputed',async()=>{
  setup('/analysis/clusters',{...payload,stale:true});
  expect(await screen.findByText('更新中')).toBeTruthy();
 });
});

describe('cluster helpers',()=>{
 it('orders by any column with missing values last and our row always first',()=>{
  const rows=[{cluster_id:'a',accounts:3,bot_share:null},{cluster_id:'b',accounts:9,bot_share:0.5},{cluster_id:'own',is_own:true,accounts:5}];
  expect(orderClusters({cluster_id:'own',accounts:5},rows,'bots').map(row=>row.cluster_id)).toEqual(['own','b','a']);
  expect(orderClusters({cluster_id:'own',accounts:5},rows,'accounts',true).map(row=>row.cluster_id)).toEqual(['own','a','b']);
  expect(orderClusters(null,rows,'accounts').map(row=>row.cluster_id)).toEqual(['b','a']);
 });

 it('finds a cluster by a Blue cluster id and builds the link back',()=>{
  expect(clusterForBlue([big,busy],'c55')?.cluster_id).toBe('c5521');
  expect(clusterForBlue([big,busy],null)).toBeUndefined();
  expect(blueLink(big)).toBe('/blue?clusters=c55,a56');
 });
});

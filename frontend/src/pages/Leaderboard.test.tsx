// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {App} from 'antd';
import Leaderboard, {assignSlots, curveOption, defaultTeams, signed} from './Leaderboard';

// The chart is replaced by its series names so the selection can be checked without echarts.
vi.mock('../common',async importOriginal=>({...await importOriginal<typeof import('../common')>(),
 Chart:({option,ariaLabel}:{option:any;ariaLabel?:string})=><section aria-label={ariaLabel}>{option.series.map((s:any)=>s.name).join(' | ')}</section>}));

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});

const now=new Date().toISOString();
const team=(rank:number,name:string,score:number,extra:Record<string,unknown>={})=>({rank,team:name,team_id:null,score,is_own:false,change_1h:10,change_6h:60,change_24h:240,
 rate_1h:10,rate_6h:10,rate_24h:10,rank_change_24h:0,speed_per_hour:10,speed_window:6,...extra});
const teams=[team(1,'placeholder',47698,{change_1h:500,speed_per_hour:588,rank_change_24h:1}),team(2,'C--',47063),team(3,'Vertex_CTN',45266),team(4,'Opus4.8Maxxing',43888),
 team(5,'Purple Hat',43272),team(6,'Sixth',40000),team(41,'Agent Zero',4615,{is_own:true,change_1h:-0,change_6h:172,change_24h:null,speed_per_hour:28.6,rank_change_24h:-2})];
const board={generated_at:now,competition_id:2,own_team:'Agent Zero',status:'ok',windows:[1,6,24],stale:false,last_failure:null,
 snapshot:{observed_at:now,first_observed_at:'2026-10-24T05:00:00.000000Z',snapshots_24h:20,teams:149,scored_teams:65,baselines:{}},teams,
 own:{...teams[6],above:{team:'Sixth',rank:6,score:40000,gap:35385,speed_per_hour:10,catch_up_hours:1902.4},first:{team:'placeholder',rank:1,score:47698,gap:43083,speed_per_hour:588,catch_up_hours:null}},
 summary:{fastest:[]}};
const series=(names:string[])=>Object.fromEntries(names.map(name=>[name,[{at:'2026-10-24T05:00:00Z',score:100},{at:'2026-10-24T06:00:00Z',score:160}]]));

function setup(payload:Record<string,unknown>) {
 const fetch=vi.fn(async(input:RequestInfo|URL)=>{
  const url=new URL(String(input),'http://localhost'),names=(url.searchParams.get('teams')||'').split('\n').filter(Boolean);
  return new Response(JSON.stringify(names.length?{...payload,series:series(names),series_hours:Number(url.searchParams.get('hours'))}:payload),{headers:{'Content-Type':'application/json'}});
 });
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><App><Leaderboard/></App></QueryClientProvider>);
 return fetch;
}
const lastTeams=(fetch:ReturnType<typeof setup>)=>new URL(String(fetch.mock.calls.at(-1)![0]),'http://localhost').searchParams.get('teams');

describe('leaderboard helpers',()=>{
 it('formats signed changes and picks our team plus the top five',()=>{
  expect([signed(12),signed(-3),signed(0),signed(null)]).toEqual(['+12','-3','0','—']);
  expect(defaultTeams(teams)).toEqual(['Agent Zero','placeholder','C--','Vertex_CTN','Opus4.8Maxxing','Purple Hat']);
 });
 it('keeps each team on its colour when others are added or removed',()=>{
  const first=assignSlots({},['Agent Zero','A','B','C'],'Agent Zero');
  expect(first).toEqual({'Agent Zero':0,A:1,B:2,C:3});
  const next=assignSlots(first,['Agent Zero','C','D'],'Agent Zero');
  expect(next).toEqual({'Agent Zero':0,C:3,D:1});
 });
 it('draws one line per team on a single axis and can index every line to its start',()=>{
  const option=curveOption(series(['B','Agent Zero']),{'Agent Zero':0,B:1},'growth','Agent Zero');
  expect(option.series.map(s=>s.name)).toEqual(['Agent Zero（我方）','B']);
  expect(option.series[1].data).toEqual([['2026-10-24T05:00:00Z',0],['2026-10-24T06:00:00Z',60]]);
  expect(option.series[0].lineStyle.color).toBe('#2a78d6');
  expect(Array.isArray(option.yAxis)).toBe(false);
 });
});

describe('leaderboard page',()=>{
 it('shows our standing, the ranking with changes, and curves for the default teams',async()=>{
  const fetch=setup(board);
  expect(await screen.findByText('我方名次')).toBeTruthy();
  expect(screen.getByText(/共 149 队，65 队有分/)).toBeTruthy();
  expect(screen.getByText(/Sixth（第 6 名，10 分\/时） · 按现在的速度约 1,902.4 小时追上/)).toBeTruthy();
  const own=screen.getAllByText('我方')[0].closest('tr')!;
  expect(own.className).toContain('board-own');
  expect(within(own).getByText('+172')).toBeTruthy();
  expect(within(own).getByText('↓2')).toBeTruthy();
  await waitFor(()=>expect(lastTeams(fetch)).toBe('Agent Zero\nplaceholder\nC--\nVertex_CTN\nOpus4.8Maxxing\nPurple Hat'));
  expect(await screen.findByText(/Agent Zero（我方） \| placeholder \| C--/)).toBeTruthy();
 },15000);

 it('adds a team to the curves from its row checkbox',async()=>{
  const fetch=setup(board);
  await screen.findByText('我方名次');
  await waitFor(()=>expect(lastTeams(fetch)).toContain('Purple Hat'));
  fireEvent.click(screen.getByRole('checkbox',{name:'在曲线里显示 Sixth'}));
  await waitFor(()=>expect(lastTeams(fetch)).toContain('Sixth'));
  fireEvent.click(screen.getByRole('checkbox',{name:'在曲线里显示 placeholder'}));
  await waitFor(()=>expect(lastTeams(fetch)).not.toContain('placeholder'));
 },15000);

 it('warns when the snapshot is old and when the last table could not be parsed',async()=>{
  const old=new Date(Date.now()-90*60000).toISOString();
  setup({...board,snapshot:{...board.snapshot,observed_at:old},last_failure:{observed_at:now,error:'排行榜表头里没有 Team 或 Score 列'}});
  expect(await screen.findByText(/排行榜已经 90 分钟没有更新/)).toBeTruthy();
  expect(screen.getByText('最近一次排行榜表格没能解析')).toBeTruthy();
 },15000);

 it('explains what to do before the first snapshot',async()=>{
  setup({status:'empty',teams:[],own:null,snapshot:null,last_failure:null});
  expect(await screen.findByText('还没有排行榜快照')).toBeTruthy();
  expect(screen.getByText(/chrome:\/\/extensions/)).toBeTruthy();
 });
});

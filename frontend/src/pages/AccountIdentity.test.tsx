// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {App} from 'antd';
import AccountIdentity from './AccountIdentity';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});

const citizen={account_id:100,username:'tessa',label:'citizen',reasons:[],figure_mentioned:false,posts_24h:1,replies_24h:1,reposts_24h:0,actions_total:3,writes_48h:3,
 stance:'undecided',last_at:'2026-10-22T05:50:00Z',last_post_id:1003,last_excerpt:'Thanks, that helps.',our_replies_24h:1,our_last_reply_at:'2026-10-22T05:40:00Z',answered_us_24h:1};
const bot={...citizen,account_id:300,username:'botty',label:'bot',reasons:['always_on','high_volume'],answered_us_24h:0,our_replies_24h:0,our_last_reply_at:null,stance:'yes'};
const figure={...citizen,account_id:8,username:'DrAmaraSingh',label:'public_figure',reasons:['verified'],figure_mentioned:true,answered_us_24h:0,our_replies_24h:0,our_last_reply_at:null};
const teammate={...citizen,account_id:6383,username:'Dr Jeff Vance',label:'teammate',reasons:[],rule_reasons:['profile','style'],answered_us_24h:0,our_replies_24h:0,our_last_reply_at:null};
const payload=(rows:unknown[])=>({generated_at:'2026-10-22T06:00:00Z',window:{hours:24},summary:{citizen:2485,bot:606,non_citizen:635,ours:9,public_figure:7},
 active_accounts:3742,citizens_answering_us:33,own_flagged:{ours:{flagged:16,total:16},teammate:{flagged:9,total:9}},rows,matched:rows.length,caveats:['标签用的是 Agent 选回复对象的同一套规则（agent_targeting），不是官方名单'],read_only:true,stale:false});

const score=(mae:number,bias:number)=>({hours:47,corr:0.944,mae,exact_hours:36,predicted_total:56,official_total:63,bias,over_hours:3,under_hours:8});
const calibration={generated_at:'2026-10-22T13:00:00Z',latest_report:'2026-10-22T12:19:10Z',hours_total:52,hours_clean:47,flags:{clean:47,outage:4,irregular:1},
 metrics:{all:score(0.277,-7),recent:score(0.292,-5)},alert:'最近 24 个干净小时：每小时误差 0.292，偏差 -5。规则少算了 NPC。',promotable:['with_single_issue'],
 variants:[{key:'live',label:'线上规则',all:score(0.277,-7),recent:score(0.292,-5),precompetition_pass:0.9793,candidate:false},
  {key:'with_single_issue',label:'加上「单议题账号」',all:score(0.25,-6),recent:score(0.25,-4),precompetition_pass:0.9756,candidate:true}],
 labels:{non_npc:108,npc:2,by_kind:{replies:{npc:1,non_npc:60},reposts:{npc:1,non_npc:0},likes:{npc:0,non_npc:48},follows:{npc:0,non_npc:0}},
  newest_npc:[{account_id:812,username:'ella_reads',hour:'2026-10-22T11:19:10Z',kind:'reposts'}],contradictions:[{end:'2026-10-20T02:09:56Z',official:2,labelled_npc:0,unlabelled:0}],misjudged_citizens:[],newest_non_npc:[{account_id:9001,username:'botfam',hour:'2026-10-22T11:19:10Z'}]},
 hours:[{start:'2026-10-22T11:19:10Z',end:'2026-10-22T12:19:10Z',flag:'clean',official_replies:0,official_views:15,official_likes:3,replies_to_us:9,predicted_npc_replies:1,platform_writes:3200}],
 method:['官方红队小时报告给出每小时 NPC 回复我们的条数。'],stale:false};

function setup(initial='/processing/bots') {
 const fetch=vi.fn(async(input:RequestInfo|URL,_options?:RequestInit)=>{
  const url=new URL(String(input),'http://local'),label=url.searchParams.get('label');
  if(url.pathname==='/api/accounts/calibration')return new Response(JSON.stringify(calibration),{headers:{'Content-Type':'application/json'}});
  const rows=[citizen,bot,figure,teammate].filter(row=>!label||row.label===label);
  return new Response(JSON.stringify(payload(rows)),{headers:{'Content-Type':'application/json'}});
 });
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={[initial]}><AccountIdentity/></MemoryRouter></App></QueryClientProvider>);
 return fetch;
}

describe('Account identification',()=>{
 it('shows each account with its label, the plain-language reasons and its contact with us',async()=>{
  setup();
  const row=(await screen.findByRole('link',{name:'@tessa'})).closest('tr') as HTMLElement;
  expect(within(row).getByText('市民 NPC')).toBeTruthy();
  expect(within(row).getByText('没有触发任何机器人或非市民规则')).toBeTruthy();
  expect(within(row).getByText('回过我们 1 次')).toBeTruthy();
  expect(within(row).getByRole('link',{name:'@tessa'}).getAttribute('href')).toBe('/posts?author_id=100');
  const botRow=screen.getByRole('link',{name:'@botty'}).closest('tr') as HTMLElement;
  expect(within(botRow).getByText('24 小时里有 20 个小时以上在发帖')).toBeTruthy();
  expect(within(botRow).getByText('一天超过 60 条，或某个小时超过 12 条')).toBeTruthy();
  const figureRow=screen.getByRole('link',{name:'@DrAmaraSingh'}).closest('tr') as HTMLElement;
  expect(within(figureRow).getByText('我方原创帖会 @')).toBeTruthy();
  expect(screen.getByText('标签用的是 Agent 选回复对象的同一套规则（agent_targeting），不是官方名单')).toBeTruthy();
  const teamRow=screen.getByRole('link',{name:'@Dr Jeff Vance'}).closest('tr') as HTMLElement;
  expect(within(teamRow).getByText('按规则会被判为非市民：')).toBeTruthy();
  expect(within(teamRow).getByText(/资料不像平台 NPC/)).toBeTruthy();
  expect(screen.getByText('我方 Agent 16/16 个、队友账号 9/9 个会被这套规则判为机器人或非市民')).toBeTruthy();
 },15000);

 it('filters by label through the address and the server',async()=>{
  const fetch=setup();
  fireEvent.click(await screen.findByText('机器人 · 606'));
  await waitFor(()=>expect(screen.queryByRole('link',{name:'@tessa'})).toBeNull(),{timeout:5000});
  expect(screen.getByRole('link',{name:'@botty'})).toBeTruthy();
  expect(fetch.mock.calls.some(([input])=>String(input).includes('label=bot'))).toBe(true);
 },15000);

 it('only reads data',async()=>{
  const fetch=setup('/processing/bots?q=tessa');
  await screen.findByRole('link',{name:'@tessa'});
  expect(fetch.mock.calls.every(([input,options])=>/^\/api\/accounts\/(identity\?|calibration)/.test(String(input))&&!(options as RequestInit|undefined)?.method)).toBe(true);
  expect(fetch.mock.calls.some(([input])=>String(input).includes('q=tessa'))).toBe(true);
 });
 it('scores the rule against the official hours and shows the certain non-NPCs',async()=>{
  setup();
  const panel=await screen.findByRole('region',{name:'识别准确率'});
  expect(await within(panel).findByText('规则偏离官方数')).toBeTruthy();
  expect(within(panel).getByText('0.277')).toBeTruthy();
  expect(within(panel).getByText('108')).toBeTruthy();
  const candidate=within(panel).getByText('加上「单议题账号」').closest('tr') as HTMLElement;
  expect(within(candidate).getByText('可上线')).toBeTruthy();
  expect(within(candidate).getByText('97.6%')).toBeTruthy();
  fireEvent.click(within(panel).getByText('确定不是 NPC 的账号 · 108'));
  expect((await within(panel).findByRole('link',{name:'@botfam'})).getAttribute('href')).toBe('/processing/bots?q=9001');
  expect(within(panel).getByText('1 个小时对不上')).toBeTruthy();
  fireEvent.click(within(panel).getByText('确定是 NPC 的账号 · 2'));
  expect((await within(panel).findByRole('link',{name:'@ella_reads'})).getAttribute('href')).toBe('/processing/bots?q=812');
  expect(within(panel).getByText(/点赞：是 0 · 不是 48/)).toBeTruthy();
 });
});

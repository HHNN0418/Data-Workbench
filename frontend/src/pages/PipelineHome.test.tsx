// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {MemoryRouter, useLocation} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import PipelineHome, {readBoard} from './PipelineHome';
import {bottleneck, flowLabel, nodeView, sidebarStatus, stageReasons} from './pipelineModel';

// The former cockpit has its own test (Overview.test.tsx); here it only has to be reachable.
vi.mock('./Overview',()=>({default:()=><p>原驾驶舱内容</p>}));

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient|undefined;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();window.localStorage.clear();});
function Location() {const location=useLocation();return <output data-testid="location">{location.pathname}{location.search}</output>;}

const at='2026-10-22T00:58:00Z';
// Field names from docs/handoff/C-api.md §1, values close to what C reported live on 2026-10-22.
const thresholds={feed_age_minutes:[5,15],counter_age_minutes:[10,30],task_backlog:[1000,5000],annotation_backlog:[2000,10000],fact_extraction_age_hours:[6,24],radar_age_hours:[3,6],
 post_share:[0.4,0.1],target_blocked_share:[0.9,0.97],failure_rate_6h:[0.3,0.6],idle_hours:2,view_cap:500};
const stages=[
 {key:'collect',label:'采集',status:'ok',headline:'Latest 1 分钟前更新',updated_at:at,metrics:{posts_total:174985,posts_last_hour:820,feed_age_minutes:1,counter_age_minutes:3,task_backlog:0,news_articles:900}},
 {key:'process',label:'处理',status:'ok',headline:'标注无积压',updated_at:at,metrics:{annotated_posts:174000,annotation_backlog:0,bot_accounts:335,pending_facts:3,fact_extraction_enabled:true,fact_extraction_age_hours:1}},
 {key:'analyze',label:'分析',status:'ok',headline:'雷达 1 小时前',updated_at:at,metrics:{undecided_posts_6h:6413,yes_share_6h:0.5,citizen_posts_6h:9000,radar_generated_at:at,radar_age_hours:1.2,blue_plan_today:null}},
 {key:'strategy',label:'策略',status:'bad',headline:'最近 50 次发布全是回复，原创帖为 0',updated_at:at,metrics:{active_facts:70,recent_actions:50,recent_posts:0,recent_replies:50,post_share:0,target_pool_6h:1200,target_blocked_share:0.42}},
 {key:'execute',label:'执行',status:'unknown',error:'OperationalError',headline:'',updated_at:null,metrics:{}},
 {key:'outcome',label:'效果',status:'bad',headline:'12 小时 26.5 分/时，低于所需 35 分/时',updated_at:at,metrics:{points_per_hour_6h:30.8,points_per_hour_12h:26.5,required_points_per_hour:35,next_poll:2,gap_to_threshold:'2385',on_track:false,total:'4615',score_stale:false,
  views_per_hour_6h:4,views_today:120,view_cap:500,changes_towards_6h:0,citizen_replies_6h:4,replies_to_us_6h:130,attribution_total:40,attribution_settled:12}}
];
const status={generated_at:at,overall:'bad',thresholds,stages};
const overview={threshold:{total:'4615',observed_at:at,snapshot_age_minutes:5,stale:false,rate_basis:'12h',rates:{'6h':{points_per_hour:30.8},'12h':{points_per_hour:26.5}},polls:[{poll:2,threshold:7000,estimated_count_at:'2026-10-24T20:11:00Z',hours_left:41.5,gap:'2385',required_points_per_hour_now:35}]}};

function setup(data:unknown=status,initial='/') {
 const fetch=vi.fn(async(input:RequestInfo|URL)=>new Response(JSON.stringify(String(input).startsWith('/api/pipeline/status')?data:overview),{headers:{'Content-Type':'application/json'}}));
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[initial]}><PipelineHome/><Location/></MemoryRouter></QueryClientProvider>);
 return fetch;
}
const metric=(label:string)=>screen.getByText(label,{selector:'.metric-label'}).closest('.metric') as HTMLElement;

describe('pipeline home',()=>{
 it('shows the six stages in data-flow order, each linking to its page',async()=>{
  const fetch=setup();
  const flow=within(await screen.findByRole('list',{name:'全链路'}));
  const nodes=flow.getAllByRole('link');
  expect(nodes.map(node=>node.getAttribute('aria-label'))).toEqual(['① 采集：正常','② 处理：正常','③ 分析：正常','④ 策略：异常','⑤ 执行：读取失败','⑥ 效果：异常']);
  expect(nodes.map(node=>node.getAttribute('href'))).toEqual(['/operations','/processing','/analysis','/strategy','/agents/pipeline','/score']);
  expect(fetch.mock.calls.some(([input])=>String(input)==='/api/pipeline/status')).toBe(true);
 });

 it('colours each node by its backend status and shows its numbers and headline',async()=>{
  setup();
  const collect=await screen.findByRole('link',{name:'① 采集：正常'});
  expect(collect.className).toContain('pipeline-ok');
  expect(within(collect).getByText('17.5万')).toBeTruthy();
  expect(within(collect).getByText('+820 / 小时')).toBeTruthy();
  expect(within(collect).getByText('Latest 1 分钟前更新')).toBeTruthy();
  const strategy=screen.getByRole('link',{name:'④ 策略：异常'});
  expect(strategy.className).toContain('pipeline-danger');
  expect(strategy.className).toContain('pipeline-bottleneck');
  expect(within(strategy).getByText('原创占比 0%')).toBeTruthy();
  expect(screen.getByRole('link',{name:'⑤ 执行：读取失败'}).className).toContain('pipeline-neutral');
 });

 it('writes the flow between stages on the arrows, with the next stage for screen readers',async()=>{
  setup();
  const flow=await screen.findByRole('list',{name:'全链路'});
  const arrows=Array.from(flow.querySelectorAll('.pipeline-arrow')).map(node=>node.textContent);
  expect(arrows).toEqual(['流向 处理：+820 帖/时','流向 分析：公民帖 9,000 / 6h','流向 策略：目标池 1,200 / 6h','流向 执行：决策 — / 6h','流向 效果：发布 — / 6h']);
 });

 it('names the first bad stage as the bottleneck and says why',async()=>{
  setup();
  const callout=(await screen.findByText('瓶颈在「策略」')).closest('[role="alert"]') as HTMLElement;
  expect(within(callout).getByText('最近 50 次发布全是回复，原创帖为 0')).toBeTruthy();
  expect(within(callout).getByText('最近 50 次发布里原创占比 0%，低于 10% 判为异常')).toBeTruthy();
  expect(within(callout).getByText('另外需要看：⑥ 效果（异常）')).toBeTruthy();
  expect(within(callout).getByRole('link',{name:'查看策略'}).getAttribute('href')).toBe('/strategy');
 });

 it('says which stage could not be read instead of showing zeros',async()=>{
  setup();
  expect(await screen.findByText('这一段读取失败（OperationalError）')).toBeTruthy();
 });

 it('shows score, poll pace and the poll gap on top, under the pace warning',async()=>{
  setup();
  expect(await screen.findByText('最近 6 小时的得分速度低于 Poll 2 需要的速度')).toBeTruthy();
  expect(within(metric('官方得分')).getByText('4,615')).toBeTruthy();
  expect(within(metric('Poll 2 速度（6 小时）')).getByText('30.8')).toBeTruthy();
  expect(within(metric('Poll 2 速度（6 小时）')).getByText('需要 35 分/时 · 12 小时 26.5 分/时')).toBeTruthy();
  expect(within(metric('距 Poll 2 门槛')).getByText('2,385')).toBeTruthy();
  expect(within(metric('距 Poll 2 门槛')).getByText('按当前速度赶不上')).toBeTruthy();
 });

 it('keeps a hand-copied leader score in this browser and shows the gap to it',async()=>{
  setup();
  await screen.findByRole('list',{name:'全链路'});
  expect(within(metric('落后榜首')).getByText('—')).toBeTruthy();
  fireEvent.click(within(metric('落后榜首')).getByRole('button',{name:'录入榜首分数'}));
  const dialog=await screen.findByRole('dialog');
  fireEvent.change(within(dialog).getAllByRole('spinbutton')[0],{target:{value:'5000'}});
  fireEvent.click(within(dialog).getByRole('button',{name:/保\s*存/}));
  await waitFor(()=>expect(within(metric('落后榜首')).getByText('385')).toBeTruthy());
  expect(readBoard()?.leader).toBe(5000);
  expect(within(metric('落后榜首')).getByRole('button',{name:'修改'})).toBeTruthy();
 },30000);

 it('draws the results feeding back into strategy with the attribution and radar in use',async()=>{
  setup();
  const loop=within(await screen.findByRole('group',{name:'效果回流到策略'}));
  expect(loop.getByText('⑥ 效果回流到 ④ 策略')).toBeTruthy();
  expect(loop.getByRole('link',{name:'归因已结算 12 / 40'}).getAttribute('href')).toBe('/results/attribution');
  expect(loop.getByRole('link',{name:/当前雷达生成于/}).getAttribute('href')).toBe('/ai/analysis');
 });

 it('shows no bottleneck when every stage is fine',async()=>{
  setup({...status,overall:'ok',stages:stages.map(stage=>({...stage,status:'ok'}))});
  await screen.findByRole('list',{name:'全链路'});
  expect(screen.queryByText(/瓶颈在/)).toBeNull();
 });

 it('keeps the former cockpit on a second tab that has its own address',async()=>{
  setup();
  await screen.findByRole('list',{name:'全链路'});
  fireEvent.click(screen.getByRole('tab',{name:'数据总览'}));
  expect(await screen.findByText('原驾驶舱内容')).toBeTruthy();
  expect(screen.getByTestId('location').textContent).toBe('/?view=data');
  cleanup();client?.clear();
  setup(status,'/?view=data');
  expect(await screen.findByText('原驾驶舱内容')).toBeTruthy();
  // The pace warning stays above both tabs.
  expect(await screen.findByText('最近 6 小时的得分速度低于 Poll 2 需要的速度')).toBeTruthy();
 });
});

describe('pipeline model',()=>{
 it('prefers bad over warn and the earlier stage on ties, and never calls an unreadable stage the bottleneck',()=>{
  expect(bottleneck([{key:'collect',status:'warn'},{key:'outcome',status:'bad'},{key:'strategy',status:'bad'}])?.key).toBe('strategy');
  expect(bottleneck([{key:'collect',status:'ok'},{key:'execute',status:'unknown'}])).toBeUndefined();
 });

 it('formats node numbers and arrow flows from the metrics, with dashes for missing values',()=>{
  expect(nodeView('strategy',{active_facts:70,post_share:0.25,target_blocked_share:0.4,target_pool_6h:12}).details).toEqual(['原创占比 25%','不可作目标 40%','目标池 12 / 6 小时']);
  expect(nodeView('collect',{}).main).toBe('—');
  expect(nodeView('analyze',{radar_age_hours:null}).details).toContain('还没有雷达');
  expect(flowLabel('collect',{collect:{metrics:{posts_last_hour:820}}})).toBe('+820 帖/时');
  expect(flowLabel('outcome',{})).toBe('');
 });

 it('explains a status from the metrics past the backend thresholds',()=>{
  expect(stageReasons('collect',{feed_age_minutes:20,counter_age_minutes:3,task_backlog:1200},thresholds)).toEqual(['距最近一次 Latest 采集 20 分钟，超过 15 分钟 判为异常','到期未完成的采集任务 1,200，超过 1,000 需要注意']);
  expect(stageReasons('execute',{failure_rate_6h:0.1,minutes_since_publish:200,posts_6h:1,replies_6h:9,post_reply_ratio_6h:0.11},thresholds)).toEqual([
   '已经 3.3 小时没有发布（队伍开着时超过 2 小时判为异常）'
  ]);
  expect(stageReasons('outcome',{points_per_hour_6h:30,points_per_hour_12h:40,required_points_per_hour:35,next_poll:2},thresholds)).toEqual(['最近 6 小时 30 分/时，低于 Poll 2 需要的 35 分/时（12 小时还够）']);
  expect(stageReasons('process',{annotation_backlog:10,fact_extraction_enabled:false,fact_extraction_age_hours:30},thresholds)).toEqual([]);
 });

 it('maps stages to sidebar groups, with outcome on results and the overall status on the home item',()=>{
  expect(sidebarStatus(undefined)).toEqual({});
  expect(sidebarStatus({overall:'warn',stages:[{key:'collect',status:'ok',headline:'好'},{key:'outcome',status:'warn'}]})).toEqual({collect:{status:'ok',headline:'好'},results:{status:'warn',headline:undefined},overview:{status:'warn'}});
 });
});

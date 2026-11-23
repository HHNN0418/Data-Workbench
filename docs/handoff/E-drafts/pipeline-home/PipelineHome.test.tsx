// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, render, screen, within} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import PipelineHome from './PipelineHome';
import {bottleneck, flowLabel, nodeView} from './pipelineModel';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
let client:QueryClient|undefined;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});

const at='2026-10-22T00:58:00Z';
// Values close to what C reported live on 2026-10-22.
const status={generated_at:at,overall:'bad',thresholds:{},stages:[
 {key:'collect',label:'采集',status:'ok',headline:'Latest 1 分钟前更新',updated_at:at,metrics:{posts_total:174985,posts_last_hour:820,feed_age_minutes:1,task_backlog:0}},
 {key:'process',label:'处理',status:'ok',headline:'标注无积压',updated_at:at,metrics:{annotated_posts:174000,annotation_backlog:0,bot_accounts:335,pending_facts:3}},
 {key:'analyze',label:'分析',status:'ok',headline:'雷达 1 小时前',updated_at:at,metrics:{undecided_posts_6h:6413,yes_share_6h:0.5,citizen_posts_6h:9000,radar_age_hours:1.2,blue_plan_today:null}},
 {key:'strategy',label:'策略',status:'bad',headline:'最近 50 次发布全是回复，原创帖为 0',updated_at:at,metrics:{active_facts:70,post_share:0,target_blocked_share:0.42,target_pool_6h:1200}},
 {key:'execute',label:'执行',status:'unknown',error:'OperationalError',headline:'',updated_at:null,metrics:{}},
 {key:'outcome',label:'效果',status:'bad',headline:'12 小时 26.5 分/时，低于所需 35 分/时',updated_at:at,metrics:{points_per_hour_6h:30.8,points_per_hour_12h:26.5,required_points_per_hour:35,next_poll:2,gap_to_threshold:'2385',on_track:false,total:'4615',attribution_total:40,attribution_settled:12,changes_towards_6h:0}}
]};

function setup(data:unknown=status) {
 vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL)=>new Response(JSON.stringify(String(input).startsWith('/api/pipeline/status')?data:{threshold:null}),{headers:{'Content-Type':'application/json'}})));
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><MemoryRouter><PipelineHome/></MemoryRouter></QueryClientProvider>);
}

describe('pipeline home',()=>{
 it('shows the six stages in data-flow order, each linking to its page',async()=>{
  setup();
  const flow=within(await screen.findByRole('list',{name:'全链路'}));
  const nodes=flow.getAllByRole('listitem');
  expect(nodes.map(node=>node.getAttribute('aria-label'))).toEqual(['① 采集：正常','② 处理：正常','③ 分析：正常','④ 策略：异常','⑤ 执行：读取失败','⑥ 效果：异常']);
  expect(nodes.map(node=>node.getAttribute('href'))).toEqual(['/operations','/processing','/analysis','/strategy','/agents/pipeline','/score']);
 });

 it('names the first bad stage as the bottleneck with the backend reason',async()=>{
  setup();
  expect(await screen.findByText('瓶颈在「策略」')).toBeTruthy();
  expect(screen.getAllByText('最近 50 次发布全是回复，原创帖为 0').length).toBeGreaterThan(0);
  expect(screen.getByRole('link',{name:'查看策略'}).getAttribute('href')).toBe('/strategy');
 });

 it('says which stage could not be read instead of showing zeros',async()=>{
  setup();
  expect(await screen.findByText('这一段读取失败（OperationalError）')).toBeTruthy();
 });

 it('shows score, speed, need and the gap on top, and the feedback loop to strategy',async()=>{
  setup();
  expect(await screen.findByText('官方得分')).toBeTruthy();
  expect(screen.getByText('按当前速度达不到')).toBeTruthy();
  expect(screen.getByRole('link',{name:/效果回流到策略：归因已结算 12 \/ 40/}).getAttribute('href')).toBe('/results/attribution');
 });

 it('shows no bottleneck when every stage is fine',async()=>{
  setup({...status,stages:status.stages.map(stage=>({...stage,status:'ok'}))});
  await screen.findByRole('list',{name:'全链路'});
  expect(screen.queryByText(/瓶颈在/)).toBeNull();
 });
});

describe('pipeline model',()=>{
 it('prefers bad over warn and the earlier stage on ties',()=>{
  expect(bottleneck([{key:'collect',status:'warn'},{key:'outcome',status:'bad'},{key:'strategy',status:'bad'}])?.key).toBe('strategy');
  expect(bottleneck([{key:'collect',status:'ok'},{key:'execute',status:'unknown'}])).toBeUndefined();
 });

 it('formats node numbers and arrow flows from the metrics',()=>{
  expect(nodeView('strategy',{active_facts:70,post_share:0.25,target_blocked_share:0.4,target_pool_6h:12}).details).toEqual(['原创占比 25%','不可作目标 40%','目标池 12 / 6 小时']);
  expect(nodeView('collect',{}).main).toBe('—');
  expect(flowLabel('collect',{collect:{metrics:{posts_last_hour:820}}})).toBe('+820/时');
 });
});

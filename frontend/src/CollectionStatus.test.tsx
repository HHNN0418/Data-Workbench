// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {cleanup,render,screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {CollectionStatusBadge,CollectionStatusDetails,type CollectionStatus} from './CollectionStatus';
import Operations from './pages/Operations';
import Score from './pages/Score';
import {useApi} from './api';
import {time} from './common';

vi.mock('./api',()=>({useApi:vi.fn(),useAction:()=>({isPending:false,mutate:vi.fn()})}));
vi.mock('./common',async importOriginal=>{
 const original=await importOriginal<typeof import('./common')>();
 return {...original,Metrics:()=>null,DataTable:()=>null,Chart:()=>null};
});
beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 const originalStyle=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>originalStyle(element));
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
afterEach(()=>{cleanup();vi.clearAllMocks();});
const healthy:CollectionStatus={state:'healthy',label:'自动同步',healthy:true,reason:'最近成功采集并已入库',last_collected_at:'2026-10-19T14:10:00Z',last_ingested_at:'2026-10-19T14:11:00Z',heartbeat_at:'2026-10-19T14:12:00Z',freshness_seconds:300};

describe('successful collection status',()=>{
 it('shows collection, database receipt and heartbeat as separate times',()=>{
  const {container}=render(<><CollectionStatusBadge status={healthy}/><CollectionStatusDetails status={{...healthy,last_attempt_at:'2026-10-19T14:13:00Z',http_status:200,activity_rows:0}}/></>);
  expect(container.querySelector('.ant-tag-green')).toBeTruthy();
  expect(screen.getByText(`最近成功采集：${time(healthy.last_collected_at)}`)).toBeTruthy();
  expect(screen.getByText(`最近入库：${time(healthy.last_ingested_at)}`)).toBeTruthy();
  expect(screen.getByText(`连接心跳：${time(healthy.heartbeat_at)}`)).toBeTruthy();
  expect(screen.getByText('最近读取流水：0 条')).toBeTruthy();
  expect(screen.getByText('HTTP 200')).toBeTruthy();
  expect(screen.getByText(/重复流水或总分未变化也会更新成功采集时间/)).toBeTruthy();
 });
 it.each([
  ['waiting_login','等待登录','请在原队伍标签页重新登录'],
  ['cooldown','冷却等待','429 限流，等待 5 分钟后重试'],
  ['buffer_full','缓存已满','本地缓存满，等待入库后恢复'],
  ['pending','待入库','已采集，仍有记录等待入库'],
  ['stale','采集停滞','超过 5 分钟没有成功采集'],
  ['error','采集失败','CSV 下载失败'],
 ])('keeps %s distinct from a healthy capture even with a recent heartbeat',(state,label,reason)=>{
  const {container}=render(<><CollectionStatusBadge status={{...healthy,state,label,reason,healthy:false}}/><CollectionStatusDetails status={{...healthy,state,label,reason,healthy:false}}/></>);
  expect(screen.getByText(`采集状态：${label}`)).toBeTruthy();
  expect(screen.getByText(reason)).toBeTruthy();
  expect(container.querySelector('.ant-tag-green')).toBeNull();
  expect(screen.getByText(/连接心跳只表示扩展在线/)).toBeTruthy();
 });
 it('revokes the healthy indication when the status refresh fails',()=>{
  const {container,rerender}=render(<CollectionStatusBadge status={healthy}/>);
  expect(container.querySelector('.ant-tag-green')).toBeTruthy();
  rerender(<><CollectionStatusBadge status={healthy} error="连接失败"/><CollectionStatusDetails status={healthy} error="连接失败"/></>);
  expect(container.querySelector('.ant-tag-green')).toBeNull();
  expect(screen.getByText('无法确认最新采集状态：连接失败')).toBeTruthy();
 });
});

const operations={database:'postgresql',scheduler:{queue:[],policy:{}},coverage:{counters:{},references:{},feeds:{'feed:latest':{}},latest:{}},runs:[],logs:[],checkpoints:[],status_codes:[]};
const score={activity:{parsed:0,unparsed:0,total:0},reconciliation:{status:'unknown',intervals:[]},breakdown:[],snapshots:[]};
function setupPage(page:'operations'|'score',status?:CollectionStatus){
 vi.mocked(useApi).mockImplementation((path:string)=>({data:path==='/operations'?operations:path==='/extension-status'?{extension_id:'a'.repeat(32),heartbeat:{at:new Date().toISOString(),auto_enabled:true,pages:[{role:'portal',enabled:true}]},collection_status:status}:path==='/score'?{...score,automatic_collection:status||{enabled:true,reason:'online'}}:{items:[]},isPending:false,isError:false,error:null,refetch:vi.fn()}) as unknown as ReturnType<typeof useApi>);
 return render(<MemoryRouter>{page==='operations'?<Operations/>:<Score/>}</MemoryRouter>);
}
describe('collection status in both data pages',()=>{
 it.each(['operations','score'] as const)('%s does not show green for an old backend that only reports online',page=>{
  const {container}=setupPage(page);
  expect(screen.getByText('采集状态：状态待核验')).toBeTruthy();
  expect(screen.getByText(/服务端尚未提供成功采集状态/)).toBeTruthy();
  expect(container.querySelector('.ant-tag-green')).toBeNull();
 });
 it.each(['operations','score'] as const)('%s surfaces a stale capture despite an online extension',page=>{
  const {container}=setupPage(page,{...healthy,state:'stale',label:'采集停滞',healthy:false,reason:'最近成功采集已经超过 5 分钟'});
  expect(screen.getByText('采集状态：采集停滞')).toBeTruthy();
  expect(screen.getByText('最近成功采集已经超过 5 分钟')).toBeTruthy();
  expect(container.querySelector('.ant-tag-green')).toBeNull();
 });
});

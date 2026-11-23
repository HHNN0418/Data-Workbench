// @vitest-environment jsdom
import type {ReactNode} from 'react';
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {IdentityDetail, IdentityTag, RiskAlert, RiskSummary, RiskTag, useOwnAccounts} from './ownAccounts';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});

const tomas={account_id:10243,name:'Tomás',hidden:2,actions:180,level:'suspend_next' as const,text:'Tomás（10243）已有 2 条帖子被隐藏，再被隐藏 1 条就可能封号；操作 180 次，账号也可能被直接举报'};
const nora={account_id:6494,name:'Nora',hidden:1,actions:303,level:'report' as const,text:'Nora（6494）已有 1 条帖子被隐藏、操作 303 次：账号再被举报一次就可能封号'};
const rows=[{account_id:10243,label:'ours',rule_label:'bot',rule_reasons:['always_on','fast_reply']},{account_id:6494,label:'ours',rule_label:'non_citizen',rule_reasons:['heavy']},{account_id:6464,label:'ours',rule_label:'citizen',rule_reasons:[]}];

function Probe({id}:{id:number}) {
 const own=useOwnAccounts();
 return <div><RiskTag risk={own.risk(id)}/><IdentityTag row={own.identity(id)}/><RiskSummary risks={own.risks}/></div>;
}

function mount(node:ReactNode) {
 const fetch=vi.fn(async(_input:RequestInfo|URL)=>new Response(JSON.stringify({own_risks:[tomas,nora],rows,read_only:true}),{headers:{'Content-Type':'application/json'}}));
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 render(<QueryClientProvider client={client}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>);
 return fetch;
}

describe('our accounts: suspension risk and identification verdict',()=>{
 it('reads our accounts once and marks the risky role with its verdict',async()=>{
  const fetch=mount(<Probe id={10243}/>);
  expect(await screen.findByText('⚠ 再隐藏 1 条可能封号')).toBeTruthy();
  expect(screen.getByText('系统识别：机器人')).toBeTruthy();
  expect(screen.getByText('2 个账号有封号风险')).toBeTruthy();
  expect(screen.getByRole('link',{name:nora.text}).getAttribute('href')).toBe('/accounts?q=6494');
  const url=new URL(String(fetch.mock.calls[0][0]),'http://local');
  expect([url.pathname,url.searchParams.get('label'),url.searchParams.get('limit')]).toEqual(['/api/accounts/identity','ours','1000']);
 });
 it('shows the verdicts for high-frequency and NPC-like accounts and no risk tag without a risk',async()=>{
  mount(<><Probe id={6494}/><Probe id={6464}/></>);
  expect(await screen.findByText('系统识别：高频 / 非市民')).toBeTruthy();
  expect(screen.getByText('系统识别：市民 NPC')).toBeTruthy();
  expect(screen.getAllByText('⚠ 再被举报可能封号')).toHaveLength(1);
 });
 it('shows the hover text, the full warning and the reasons behind a verdict',async()=>{
  render(<MemoryRouter><RiskTag risk={tomas}/><RiskAlert risk={nora}/><IdentityDetail row={rows[0]} accountId={10243}/></MemoryRouter>);
  fireEvent.mouseEnter(screen.getByText('⚠ 再隐藏 1 条可能封号'));
  expect(await screen.findByText(tomas.text)).toBeTruthy();
  expect(screen.getByText(nora.text)).toBeTruthy();
  expect(screen.getByText(/公开数据分不清临时隐藏和永久隐藏/)).toBeTruthy();
  expect(screen.getByText('24 小时里有 20 个小时以上在发帖')).toBeTruthy();
  expect(screen.getByText('回复的中位时间不到 2 分钟')).toBeTruthy();
  expect(screen.getByRole('link',{name:'账号识别 ↗'}).getAttribute('href')).toBe('/processing/bots?label=ours&q=10243');
 });
 it('says when an account has no verdict yet and renders nothing without data',()=>{
  const {container}=render(<MemoryRouter><RiskTag/><RiskAlert/><RiskSummary risks={[]}/><IdentityTag/></MemoryRouter>);
  expect(container.textContent).toBe('');
  render(<MemoryRouter><IdentityDetail accountId={5}/></MemoryRouter>);
  expect(screen.getByText('最近 24 小时没有活动，暂时没有识别结果。')).toBeTruthy();
 });
});

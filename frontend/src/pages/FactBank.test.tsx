// @vitest-environment jsdom
import {afterEach,beforeAll,describe,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter,useLocation} from 'react-router-dom';
import FactBank from './FactBank';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});
const pending={id:'n:1',layer:'news',status:'pending',claim:'Kingstonia confirmed its first H2D2 case.',concerns:['not_here_yet'],source_label:'Kingstonia Herald, 2026-10-21',observed_at:'2026-10-21T08:30:00Z',evidence_quote:'The health ministry has confirmed the first case.',revision:1};
let location='';
function Location(){location=useLocation().search;return null;}
function setup(url='/ai/facts'){
 let settings={review_mode:'manual',extract_enabled:false,provider_id:'',pipeline:'single',revision:1};
 const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{
  const path=String(input);
  if(path==='/api/bootstrap')return new Response(JSON.stringify({csrf_token:'token'}));
  if(path==='/api/agents/facts/settings'&&options?.method==='POST'){const body=JSON.parse(String(options.body));settings={...settings,...body,revision:settings.revision+1};}
  if(path.startsWith('/api/agents/facts/settings'))return new Response(JSON.stringify({settings,providers:[],concerns:['not_here_yet','hospital'],topics:['poll:referendum']}));
  if(path.startsWith('/api/agents/facts/list'))return new Response(JSON.stringify({items:path.includes('status=pending')?[pending]:[],total:path.includes('status=pending')?1:0,counts:{pending:1,active:34}}));
  if(path==='/api/agents/facts/decision')return new Response(JSON.stringify({...pending,status:'active'}));
  if(path.startsWith('/api/agents/facts/gaps'))return new Response(JSON.stringify({brief_id:'b1',generated_at:'2026-10-22T01:00:00Z',expired:false,gaps:[{concern:'venue_list',need:'A fact on which venues are designated.'}]}));
  if(path.startsWith('/api/agents/facts/prompts'))return new Response(JSON.stringify({pipeline:'staged',placeholders:{'{name}':'角色名'},prompts:[{key:'decide',label:'决策',applies_to:'staged',text:'You are the decision step for {name}.',appendix:[{label:'输出被截断后重试时追加',text:'Retry note.'}]}]}));
  if(path.startsWith('/api/agents/facts/extractions'))return new Response(JSON.stringify({interval_seconds:300,articles_per_run:3,totals:{runs:1,accepted:2,dropped:1,failed:0},items:[{article_key:'a1',status:'done',accepted:2,dropped:1,attempts:1,updated_at:'2026-10-22T01:00:00Z',site:'Kingstonia Herald',title:'Clinics open in Crown Harbour',published_at:'2026-10-22T00:00:00Z'}]}));
  return new Response(JSON.stringify({rebuttals:[],guardrails:[]}));
 });
 vi.stubGlobal('fetch',fetch);client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[url]}><FactBank/><Location/></MemoryRouter></QueryClientProvider>);
 return fetch;
}
const posts=(fetch:ReturnType<typeof vi.fn>,path:string)=>fetch.mock.calls.filter(call=>String(call[0])===path&&call[1]?.method==='POST').map(call=>JSON.parse(String(call[1]?.body)));

describe('fact bank',()=>{
 it('saves the review mode with its revision',{timeout:20000},async()=>{
  const fetch=setup();
  fireEvent.click(await screen.findByText('自动生效'));
  expect(await screen.findByText(/已在待审中的事实不会自动生效/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'保存设置'}));
  await waitFor(()=>expect(posts(fetch,'/api/agents/facts/settings')).toEqual([{review_mode:'auto',extract_enabled:false,provider_id:'',expected_revision:1,pipeline:'single'}]));
 });
 it('shows the effective prompts read-only',{timeout:20000},async()=>{
  const fetch=setup();
  fireEvent.click(await screen.findByText('Prompt（只读）'));
  expect(await screen.findByText('You are the decision step for {name}.')).toBeTruthy();
  expect(screen.getByText('Retry note.')).toBeTruthy();
  expect(screen.queryByRole('textbox',{name:'决策'})).toBeNull();
  expect(fetch.mock.calls.some(call=>String(call[0]).startsWith('/api/agents/facts/prompts')&&call[1]?.method==='POST')).toBe(false);
 });
 it('shows fact gaps suggested by the concern radar',{timeout:20000},async()=>{
  setup();
  expect(await screen.findByText('A fact on which venues are designated.')).toBeTruthy();
 });
 it('lists pending facts with counts and approves one',{timeout:20000},async()=>{
  const fetch=setup();
  expect(await screen.findByText('待审 1')).toBeTruthy();
  expect(await screen.findByText(pending.claim)).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'通过'}));
  await waitFor(()=>expect(posts(fetch,'/api/agents/facts/decision')).toEqual([{id:'n:1',action:'approve',expected_revision:1}]));
 });
 it('opens the tab named in the URL and keeps the URL in sync',{timeout:20000},async()=>{
  setup('/ai/facts?tab=prompts');
  expect(await screen.findByText('You are the decision step for {name}.',{},{timeout:5000})).toBeTruthy();
  fireEvent.click(screen.getByText('反驳与禁止说法'));
  await waitFor(()=>expect(location).toBe('?tab=rules'),{timeout:5000});
 });
 it('shows recent extraction runs on the extraction tab',{timeout:20000},async()=>{
  setup('/ai/facts?tab=extraction');
  expect(await screen.findByText('Clinics open in Crown Harbour',{},{timeout:5000})).toBeTruthy();
  expect(screen.getByText('2 / 1')).toBeTruthy();
  expect(screen.getByText('自动提取：关闭')).toBeTruthy();
 });
 it('falls back to the pending tab for an unknown tab value',{timeout:20000},async()=>{
  setup('/ai/facts?tab=nope');
  expect(await screen.findByText(pending.claim,{},{timeout:5000})).toBeTruthy();
 });
});

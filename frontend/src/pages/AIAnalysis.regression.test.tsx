// @vitest-environment jsdom
import {afterEach, describe, expect, it, vi} from 'vitest';
import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter} from 'react-router-dom';
import AIAnalysis from './AIAnalysis';

// Keep real React state, router and API hooks. Host controls are simplified so
// these request-lifecycle regressions do not depend on Ant Design layout.
vi.mock('antd',()=>{
 const wrap=({children}:any)=><div>{children}</div>;
 return {
  Alert:({title,description}:any)=><div role="alert">{title}{description}</div>,
  Button:({children,onClick,disabled}:any)=><button disabled={disabled} onClick={onClick}>{children}</button>,
  Collapse:({items}:any)=><div>{items.map((item:any)=><section key={item.key}>{item.label}{item.children}</section>)}</div>,
  Empty:({description}:any)=><div>{description}</div>,
  Form:Object.assign(wrap,{Item:wrap}),
  Input:{TextArea:({value,onChange,disabled,'aria-label':label}:any)=><textarea aria-label={label} value={value} disabled={disabled} onChange={onChange}/>},
  Pagination:()=>null,
  Select:({options,value,onChange,disabled,id,'aria-label':label}:any)=><select id={id} aria-label={label} value={value??''} disabled={disabled} onChange={event=>onChange(event.target.value)}>{options.map((option:any)=><option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}</select>,
  Space:wrap,
  Switch:({checked,onChange,id,'aria-label':label}:any)=><input id={id} aria-label={label} type="checkbox" checked={checked} onChange={event=>onChange(event.target.checked)}/>,
  Table:()=>null,
  Tabs:({items,activeKey,onChange}:any)=><div><div role="tablist">{items.map((item:any)=><button key={item.key} role="tab" aria-selected={activeKey===item.key} onClick={()=>onChange(item.key)}>{item.label}</button>)}</div>{items.find((item:any)=>item.key===activeKey)?.children}</div>,
  Tag:wrap,
  Typography:{Text:wrap},
 };
});
vi.mock('./AccountScreening',()=>({default:()=>null}));

const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
const textProvider={id:'writer',name:'文字服务',protocol:'openai_chat',enabled:true,has_key:true,model:'writer',is_default:true};
const retrievalProvider={id:'vector',name:'向量服务',protocol:'openai_embeddings',enabled:true,has_key:true,model:'vector'};
const answer={text:'已完成的回答',sources:[],model:'writer',local_only:false,retrieval_only:false};
const chatJob={id:'chat-job',kind:'chat',scope:'',state:'running',stage:'thinking',created_at:'2026-10-21T00:00:00Z',result:null};
function response(data:unknown,status=200){return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});}
function setup(options:{retrievalOnly?:boolean;cloudRetrieval?:boolean}={}){
 let finish!:(value:Response)=>void;
 const pending=new Promise<Response>(resolve=>{finish=resolve;});
 const providers=[...(options.retrievalOnly?[]:[textProvider]),...(options.cloudRetrieval?[retrievalProvider]:[])];
 const fetch=vi.fn((input:RequestInfo|URL,_options?:RequestInit)=>{
  const path=String(input);
  if(path==='/api/ai/chat')return Promise.resolve(response(chatJob,202));
  if(path==='/api/ai/jobs/chat-job')return pending;
  if(path.startsWith('/api/ai/jobs?'))return Promise.resolve(response({items:[]}));
  if(path==='/api/bootstrap')return Promise.resolve(response({csrf_token:'fixture'}));
  return Promise.resolve(response({providers,default_provider_id:options.retrievalOnly?null:textProvider.id}));
 });
 vi.stubGlobal('fetch',fetch);
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 const tree=(scope:Record<string,unknown>)=><QueryClientProvider client={client}><MemoryRouter><AIAnalysis params={scope}/></MemoryRouter></QueryClientProvider>;
 const view=render(tree({own:'all'}));
 return {fetch,finish,rerender:(scope:Record<string,unknown>)=>view.rerender(tree(scope))};
}
async function send(fetch:ReturnType<typeof vi.fn>){
 await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path==='/api/ai/providers')).toBe(true));
 await waitFor(()=>expect(document.querySelector('option[value="writer"], option[value="vector"]')).not.toBeNull());
 fireEvent.click(screen.getByRole('tab',{name:'资料聊天'}));
 fireEvent.change(screen.getByLabelText('向帖子资料提问'),{target:{value:'原始问题'}});
 fireEvent.click(screen.getByRole('button',{name:'发送问题'}));
 await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path==='/api/ai/chat')).toBe(true));
 // A running backend job polls once per second. Wait for the actual status
 // GET before resolving its body, so the deferred response models the real
 // request contract rather than a synchronous /ai/chat result.
 await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path==='/api/ai/jobs/chat-job')).toBe(true),{timeout:2000});
 const start=fetch.mock.calls.find(([path])=>path==='/api/ai/chat')!;
 expect(start[1]?.headers).toMatchObject({'X-CTN-Job-Key':expect.any(String)});
}
async function settle(finish:(value:Response)=>void,data=answer,status=200){
 const failed=status>=400;
 await act(async()=>{finish(response({...chatJob,state:failed?'failed':'completed',stage:'complete',result:failed?null:data,...(failed?{error:(data as any).error}:{} )}));await new Promise(resolve=>setTimeout(resolve,0));});
}

describe('chat lifecycle regressions',()=>{
 it.each(['mode','instruction'])('preserves a pending chat answer when unrelated workbench %s changes',async change=>{
  const {fetch,finish}=setup();await send(fetch);
  fireEvent.click(screen.getByRole('tab',{name:'分析工作台'}));
  if(change==='mode')fireEvent.click(screen.getByRole('button',{name:'生成文章'}));
  else fireEvent.change(screen.getByLabelText('分析要求或草稿'),{target:{value:'另一个任务的要求'}});
  await settle(finish);
  fireEvent.click(screen.getByRole('tab',{name:'资料聊天'}));
  expect(await screen.findByText(answer.text)).toBeTruthy();
  expect(screen.getByText('原始问题')).toBeTruthy();
 });
 it('keeps chat errors visible and restores the question after workbench edits',async()=>{
  const {fetch,finish}=setup();await send(fetch);
  fireEvent.click(screen.getByRole('tab',{name:'分析工作台'}));
  fireEvent.click(screen.getByRole('button',{name:'生成文章'}));
  await settle(finish,{error:'服务暂不可用'} as any,502);
  fireEvent.click(screen.getByRole('tab',{name:'资料聊天'}));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent','服务暂不可用');
  expect(screen.getByLabelText('向帖子资料提问')).toHaveProperty('value','原始问题');
 });
 it.each(['scope','retrieval'])('still discards a reply after its actual %s changes',async change=>{
  const {fetch,finish,rerender}=setup({cloudRetrieval:true});await send(fetch);
  if(change==='scope')rerender({own:'own'});
  else fireEvent.change(screen.getByLabelText('语义检索服务'),{target:{value:''}});
  await settle(finish);
  await waitFor(()=>expect((screen.getByLabelText('向帖子资料提问') as HTMLTextAreaElement).disabled).toBe(false));
  expect(screen.queryByText(answer.text)).toBeNull();
  expect(screen.queryByText('原始问题')).toBeNull();
 });
 it('labels cloud retrieval without text generation as retrieval, not a generated AI answer',async()=>{
  const {fetch,finish}=setup({retrievalOnly:true,cloudRetrieval:true});await send(fetch);
  expect(screen.getByText('使用所选检索服务，返回原文依据；不调用文字生成模型')).toBeTruthy();
  const call=fetch.mock.calls.find(([path])=>path==='/api/ai/chat') as unknown as [string,RequestInit];
  const payload=JSON.parse(String(call[1].body));
  expect(payload).toMatchObject({embedding_provider_id:'vector'});
  expect(payload).not.toHaveProperty('provider_id');
  await settle(finish,{text:'检索原文',sources:[],local_only:false,retrieval_only:true} as any);
  expect(await screen.findByText('检索助手')).toBeTruthy();
  expect(screen.queryByText('AI 助手')).toBeNull();
 });
});

// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, configure, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {MemoryRouter, useLocation} from 'react-router-dom';
import AIAnalysis, {aiHistory, aiScope} from './AIAnalysis';
import AISettings from './AISettings';

vi.mock('antd',async importOriginal=>{
 const original=await importOriginal<typeof import('antd')>();
 return {...original,Select:({options,value,onChange,id,disabled,'aria-label':label}:any)=><select id={id} aria-label={label} disabled={disabled} value={value??''} onChange={event=>onChange(event.target.value)}>{(options||[]).map((option:any)=><option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}</select>};
});
beforeAll(()=>{
 configure({asyncUtilTimeout:10000});
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 const originalStyle=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>originalStyle(element));
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn().mockImplementation(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
});
const clients:QueryClient[]=[];
afterEach(()=>{cleanup();clients.forEach(client=>client.clear());clients.length=0;vi.unstubAllGlobals();});
const provider={id:'provider-a',name:'写作服务',base_url:'https://ai.example.com/v1',model:'writing-model',protocol:'openai_chat',has_key:true,enabled:true,is_default:true,token_parameter:'max_tokens'};
const source={id:'post:42',kind:'post',post_id:42,title:'如何引导回复',text:'原帖样本正文',group:'popular',metrics:{like_count:30}};
const evidence={sources:[source],summary:{post_sample_count:1,news_selected_count:0,scope_count:100,analyzed_count:100,popular_count:1,comparison_count:0,internal_counter:42},features:[{name:'提问结尾',popular:'100%',comparison:'无样本',note:'样本较少'}],caveats:['样本规律不代表因果'],prompt_template:'依据 [post:42]，为知识分享生成三个开头。'};
const result={text:'这是基于 [post:42] 的分析建议。',sources:[source],model:'writing-model',generated_at:'2026-10-20T00:00:00Z',context_pack:{estimated_tokens:3200,input_budget_tokens:8000,selected_sources:1,available_sources:100},retrieval:{method:'hybrid',semantic_search:'not_configured',reranker:'not_configured'}};
function Location(){const location=useLocation();return <output data-testid="location">{location.pathname+location.search}</output>;}
function setup({configured=false,settingsPage=false,providerProtocol='openai_chat',providerOverrides={},params={own:'own',author_id:'7',window_minutes:30,focus_post_id:42},runResponse,previewData=evidence,extraProviders=[]}: {configured?:boolean;settingsPage?:boolean;providerProtocol?:string;providerOverrides?:Record<string,unknown>;params?:Record<string,unknown>;runResponse?:()=>Promise<Response>;previewData?:object;extraProviders?:Record<string,unknown>[]}={}){
 const settings={providers:[...(configured?[{...provider,protocol:providerProtocol,...providerOverrides}]:[]),...extraProviders],default_provider_id:configured?provider.id:null};
 const fetch=vi.fn((input:RequestInfo|URL,options?:RequestInit)=>{
  const path=String(input),body=options?.body?JSON.parse(String(options.body)):{};
  if(path==='/api/ai/jobs'&&runResponse)return runResponse().then(async response=>response.ok?new Response(JSON.stringify({id:'test-job',state:'completed',stage:'已完成',result:await response.json()}),{status:202}):response);
  const data=path==='/api/bootstrap'?{csrf_token:'csrf-test'}:path==='/api/ai/preview'?previewData:path==='/api/ai/jobs'?{...result,...(body.mode==='write'?{steps:[{name:'热门写法研究',text:'研究结果'},{name:'完整文章',text:'完整成稿'}]}:{})}:path==='/api/ai/chat'?{...result,local_only:!body.provider_id}:path==='/api/ai/account-screening'?{items:[],summary:{}}:settings;
  return Promise.resolve(new Response(JSON.stringify(['/api/ai/jobs','/api/ai/chat','/api/ai/providers/test'].includes(path)?{id:'test-job',state:'completed',stage:'已完成',result:data}:data),{status:['/api/ai/jobs','/api/ai/chat','/api/ai/providers/test'].includes(path)?202:200,headers:{'Content-Type':'application/json'}}));
 });
 vi.stubGlobal('fetch',fetch);
 const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});clients.push(client);
 const tree=(scope:Record<string,unknown>)=><QueryClientProvider client={client}><MemoryRouter initialEntries={[settingsPage?'/ai-settings':'/analysis?analysis_tab=ai']}>{settingsPage?<AISettings/>:<AIAnalysis params={scope}/>}<Location/></MemoryRouter></QueryClientProvider>;
 const view=render(tree(params));
 return {fetch,rerender:(scope:Record<string,unknown>)=>view.rerender(tree(scope))};
}
function posted(fetch:ReturnType<typeof vi.fn>,path:string){return fetch.mock.calls.filter(([url])=>url===path).map(([,options])=>JSON.parse(String(options.body)));}

describe('AI evidence and execution boundaries',()=>{
 it('uses only the supported scope and bounds recent chat context',()=>{
  expect(aiScope({own:'own',author_id:'7',since:'yesterday',window_minutes:30,sample_batch:'a',focus_post_id:42,analysis_tab:'ai'})).toEqual({own:'own',author_id:'7',since:'yesterday'});
  const history=aiHistory(Array.from({length:14},(_,index)=>({role:index%2?'assistant' as const:'user' as const,content:`${index}:`+'x'.repeat(13000)})));
  expect(history.length).toBeLessThanOrEqual(8);expect(history.reduce((sum,item)=>sum+item.content.length,0)).toBeLessThanOrEqual(24000);expect(history.every(item=>item.content.length<=12000)).toBe(true);expect(history.at(-1)?.role).toBe('assistant');
 });
 it('requires a configured text AI and keeps optional evidence preview separate',async()=>{
  const {fetch}=setup();await screen.findByRole('button',{name:'分析热门文章'});
  expect((screen.getByRole('button',{name:/分析热门文章/}) as HTMLButtonElement).disabled).toBe(true);
  expect(posted(fetch,'/api/ai/jobs')).toEqual([]);expect(posted(fetch,'/api/ai/preview')).toEqual([]);
  expect(screen.getByRole('button',{name:'热门文章分析'}).getAttribute('aria-pressed')).toBe('true');
  expect(screen.queryByRole('tab',{name:'创作 Agent'})).toBeNull();expect(screen.queryByRole('tab',{name:'账号筛查'})).toBeNull();
  expect(screen.queryByRole('tab',{name:'API 设置'})).toBeNull();
  expect(screen.queryByRole('button',{name:'预览资料'})).toBeNull();
  fireEvent.click(screen.getByText('查看取材预览（可选）'));fireEvent.click(await screen.findByRole('button',{name:'预览资料'}));
  expect(await screen.findByText('本地证据')).toBeTruthy();
  expect(screen.queryByText(evidence.prompt_template)).toBeNull();expect(screen.queryByText('复制指令')).toBeNull();expect(screen.queryByText('internal_counter')).toBeNull();
  expect(posted(fetch,'/api/ai/preview')).toEqual([{params:{author_id:'7',own:'own'},mode:'popular',instruction:'',sample_size:100,include_news:true}]);
  const previewRequest=fetch.mock.calls.find(([url])=>url==='/api/ai/preview')?.[1];expect(previewRequest?.headers).toMatchObject({'X-CTN-Token':'csrf-test'});
  fireEvent.click(screen.getByText('查看引用资料（1 条）'));
  expect((await screen.findByRole('link',{name:'原帖 #42 ↗'})).getAttribute('href')).toBe('/posts?post=42');
  expect(posted(fetch,'/api/ai/jobs')).toEqual([]);
  fireEvent.click(screen.getByRole('button',{name:'配置 AI 服务'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toBe('/ai/settings'));
 },30000);
 it('runs all three tasks directly with the selected external AI and no preview prerequisite',async()=>{
  const {fetch}=setup({configured:true});await waitFor(()=>expect((screen.getByRole('button',{name:'分析热门文章'}) as HTMLButtonElement).disabled).toBe(false));
  expect(posted(fetch,'/api/ai/jobs')).toEqual([]);
  const tasks=[['热门文章分析','分析热门文章','popular'],['生成文章','生成完整文章','write'],['举报线索','查找举报线索','reporting']];
  for(const [index,[title,action,mode]] of tasks.entries()){
   fireEvent.click(screen.getByRole('button',{name:title}));fireEvent.change(screen.getByLabelText('分析要求或草稿'),{target:{value:'面向新手，避免夸大'}});
   fireEvent.click(screen.getByRole('button',{name:action}));expect(await screen.findByText(result.text)).toBeTruthy();
   expect(posted(fetch,'/api/ai/jobs')[index]).toMatchObject({mode,instruction:'面向新手，避免夸大',provider_id:'provider-a',agent:false,sample_size:100,include_news:true});
   if(mode==='write')expect(screen.getByText('2. 完整文章')).toBeTruthy();
  }
  expect(posted(fetch,'/api/ai/preview')).toEqual([]);expect(posted(fetch,'/api/ai/account-screening')).toEqual([]);
  expect(screen.getByText('引用资料 1 条')).toBeTruthy();expect(screen.getByText('证据约 3,200 / 8,000 tokens')).toBeTruthy();
  expect(screen.queryByText('available_sources')).toBeNull();fireEvent.click(screen.getByText('取材与上下文预算'));expect(await screen.findByText(/available_sources/)).toBeTruthy();
 },30000);
 it('maps the legacy screening URL to reporting and passes only time filters to optional local checks',async()=>{
  const {fetch}=setup({configured:true,params:{ai_view:'screening',own:'own',author_id:'7',since:'2026-10-01',until:'2026-10-20'}});
  expect(screen.getByRole('button',{name:'举报线索'}).getAttribute('aria-pressed')).toBe('true');
  expect(posted(fetch,'/api/ai/account-screening')).toEqual([]);
  fireEvent.click(screen.getByText('本地详细检查（可选）'));fireEvent.click(await screen.findByRole('button',{name:'扫描账号'}));
  await waitFor(()=>expect(posted(fetch,'/api/ai/account-screening')).toHaveLength(1));
  expect(posted(fetch,'/api/ai/account-screening')[0].params).toEqual({since:'2026-10-01',until:'2026-10-20'});
 },30000);
 it('supports local retrieval chat and clears old conversation when scope changes',async()=>{
  const {fetch,rerender}=setup();fireEvent.click(screen.getByRole('tab',{name:'资料聊天'}));fireEvent.change(screen.getByLabelText('向帖子资料提问'),{target:{value:'怎样引导回复？'}});fireEvent.click(screen.getByRole('button',{name:'发送问题'}));
  expect(await screen.findByText(result.text)).toBeTruthy();expect(posted(fetch,'/api/ai/chat')[0]).toEqual({params:{author_id:'7',own:'own'},question:'怎样引导回复？',history:[],sample_size:100,include_news:true});
  fireEvent.change(screen.getByLabelText('向帖子资料提问'),{target:{value:'还有哪些结构？'}});fireEvent.click(screen.getByRole('button',{name:'发送问题'}));await waitFor(()=>expect(posted(fetch,'/api/ai/chat')).toHaveLength(2));
  expect(posted(fetch,'/api/ai/chat')[1].history).toEqual([{role:'user',content:'怎样引导回复？'},{role:'assistant',content:result.text}]);
  rerender({own:'all',author_id:'9'});await waitFor(()=>expect(screen.queryByText('怎样引导回复？')).toBeNull());expect(screen.queryByText(result.text)).toBeNull();
 },30000);
 it('lets users change post and news evidence controls and clears previous evidence',async()=>{
  const {fetch}=setup();fireEvent.click(screen.getByText('查看取材预览（可选）'));fireEvent.click(await screen.findByRole('button',{name:'预览资料'}));await screen.findByText('本地证据');
  fireEvent.click(screen.getByText('资料设置 · 最多 100 帖 + 新闻'));fireEvent.change(await screen.findByLabelText('帖子样本数'),{target:{value:'200'}});
  await waitFor(()=>expect(screen.queryByText('本地证据')).toBeNull());
  fireEvent.click(screen.getByRole('button',{name:'生成文章'}));fireEvent.change(screen.getByLabelText('分析要求或草稿'),{target:{value:'比较最近帖子的开头'}});fireEvent.click(screen.getByLabelText('补充新闻依据'));
  fireEvent.click(screen.getByRole('button',{name:'预览资料'}));await screen.findByText('本地证据');
  expect(posted(fetch,'/api/ai/preview').at(-1)).toMatchObject({mode:'write',sample_size:200,include_news:false,instruction:'比较最近帖子的开头'});
  fireEvent.change(screen.getByLabelText('帖子样本数'),{target:{value:'40'}});await waitFor(()=>expect(screen.queryByText('本地证据')).toBeNull());
  expect(posted(fetch,'/api/ai/jobs')).toHaveLength(0);
 },30000);
 it('rejects an old cloud response after its evidence settings change',async()=>{
  let finish:((response:Response)=>void)|undefined;
  const {rerender}=setup({configured:true,runResponse:()=>new Promise(resolve=>{finish=resolve;})});
  await waitFor(()=>expect((screen.getByRole('button',{name:'分析热门文章'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByText('资料设置 · 最多 100 帖 + 新闻'));fireEvent.click(screen.getByRole('button',{name:'分析热门文章'}));
  await waitFor(()=>expect((screen.getByRole('button',{name:/分析热门文章/}) as HTMLButtonElement).disabled).toBe(true));
  await waitFor(()=>expect(finish).toBeTypeOf('function'));
  fireEvent.change(screen.getByLabelText('帖子样本数'),{target:{value:'40'}});finish!(new Response(JSON.stringify(result),{status:200}));
  await waitFor(()=>expect((screen.getByRole('button',{name:'分析热门文章'}) as HTMLButtonElement).disabled).toBe(false));expect(screen.queryByText(result.text)).toBeNull();
  rerender({own:'all'});
 },30000);
 it('paginates source material instead of expanding all selected posts',async()=>{
  const sources=Array.from({length:10},(_,index)=>({...source,id:`post:${100+index}`,post_id:100+index,title:`样本 ${index+1}`,text:`样本正文 ${index+1}`}));
  setup({previewData:{...evidence,sources}});fireEvent.click(screen.getByText('查看取材预览（可选）'));fireEvent.click(await screen.findByRole('button',{name:'预览资料'}));fireEvent.click(await screen.findByText('查看引用资料（10 条）'));
  expect(await screen.findByText('样本 1')).toBeTruthy();expect(screen.queryByText('样本 9')).toBeNull();
  fireEvent.click(screen.getByTitle('2'));expect(await screen.findByText('样本 9')).toBeTruthy();expect(screen.queryByText('样本 1')).toBeNull();
 },30000);
 it('ignores an old cloud response after the scope changes',async()=>{
  let finish:((response:Response)=>void)|undefined;
  const {rerender}=setup({configured:true,runResponse:()=>new Promise(resolve=>{finish=resolve;})});
  await waitFor(()=>expect((screen.getByRole('button',{name:'分析热门文章'}) as HTMLButtonElement).disabled).toBe(false));fireEvent.click(screen.getByRole('button',{name:'分析热门文章'}));
  await waitFor(()=>expect((screen.getByRole('button',{name:/分析热门文章/}) as HTMLButtonElement).disabled).toBe(true));
  await waitFor(()=>expect(finish).toBeTypeOf('function'));
  rerender({own:'all',author_id:'9'});finish!(new Response(JSON.stringify(result),{status:200}));
  await waitFor(()=>expect((screen.getByRole('button',{name:'分析热门文章'}) as HTMLButtonElement).disabled).toBe(false));expect(screen.queryByText(result.text)).toBeNull();
 },30000);
 it('preserves an existing secret when editing metadata and retains the compatibility setting',async()=>{
  const {fetch}=setup({configured:true,settingsPage:true});fireEvent.click(await screen.findByRole('button',{name:/编\s*辑/}));
  const keyInput=screen.getByLabelText('API Key（留空保留原密钥）') as HTMLInputElement;expect(keyInput.value).toBe('');
  fireEvent.change(screen.getByLabelText('配置名称'),{target:{value:'新的服务名称'}});fireEvent.click(screen.getByRole('button',{name:'保存 API 配置'}));
  await waitFor(()=>expect(posted(fetch,'/api/ai/providers/save')).toHaveLength(1));
  expect(posted(fetch,'/api/ai/providers/save')[0]).toMatchObject({id:'provider-a',name:'新的服务名称',model:'writing-model',protocol:'openai_chat',thinking_mode:'auto',max_output_tokens:null});expect(posted(fetch,'/api/ai/providers/save')[0]).not.toHaveProperty('api_key');
  expect(posted(fetch,'/api/ai/providers/test')).toHaveLength(0);
 },30000);
 it('keeps native account-review providers out of general content-analysis selection',async()=>{
  const {fetch}=setup({configured:true,providerProtocol:'typesafe'});await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path==='/api/ai/providers')).toBe(true));
  expect((screen.getByRole('button',{name:/分析热门文章/}) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByLabelText('当前 AI 服务') as HTMLSelectElement).options.length).toBe(1);expect(posted(fetch,'/api/ai/jobs')).toHaveLength(0);
 },30000);
 it('saves native TypeSafe settings with its protocol and canonical defaults',async()=>{
  const {fetch}=setup({settingsPage:true});fireEvent.change(await screen.findByLabelText('服务类型'),{target:{value:'typesafe'}});
  expect((screen.getByLabelText('API Base URL') as HTMLInputElement).value).toBe('https://api.typesafe.ai/v1');expect((screen.getByLabelText('模型 ID') as HTMLInputElement).value).toBe('jev-latest');
  fireEvent.change(screen.getByLabelText('配置名称'),{target:{value:'可选账号复核'}});fireEvent.change(screen.getByLabelText('API Key（本机服务可留空）'),{target:{value:'test-placeholder-key'}});fireEvent.click(screen.getByRole('button',{name:'保存 API 配置'}));
  await waitFor(()=>expect(posted(fetch,'/api/ai/providers/save')).toHaveLength(1));expect(posted(fetch,'/api/ai/providers/save')[0]).toMatchObject({protocol:'typesafe',base_url:'https://api.typesafe.ai/v1',model:'jev-latest'});
  expect(posted(fetch,'/api/ai/providers/save')[0]).not.toHaveProperty('thinking_mode');expect(posted(fetch,'/api/ai/providers/save')[0]).not.toHaveProperty('max_output_tokens');
  expect(posted(fetch,'/api/ai/providers/test')).toHaveLength(0);
 },30000);
 it('allows thinking without exposing a saved key or a local output budget',async()=>{
  const {fetch}=setup({configured:true,settingsPage:true,providerOverrides:{thinking_mode:'enabled',max_output_tokens:4096}});
  fireEvent.click(await screen.findByRole('button',{name:/编\s*辑/}));
  await screen.findByLabelText('思考模式');
  expect(screen.queryByLabelText('最大输出 Token 数')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'保存 API 配置'}));
  await waitFor(()=>expect(posted(fetch,'/api/ai/providers/save')).toHaveLength(1));
  expect(posted(fetch,'/api/ai/providers/save')[0]).toMatchObject({thinking_mode:'enabled',max_output_tokens:null});
  expect(posted(fetch,'/api/ai/providers/save')[0]).not.toHaveProperty('api_key');
 },30000);
 it('keeps cloud retrieval roles separate and sends their selected IDs for tasks and chat',async()=>{
  const {fetch}=setup({configured:true,extraProviders:[{...provider,id:'embedding-a',name:'向量服务',model:'embedding-model',protocol:'openai_embeddings'},{...provider,id:'rerank-a',name:'重排服务',model:'rerank-model',protocol:'rerank'},{...provider,id:'review-a',name:'复核服务',protocol:'typesafe'}]});
  await waitFor(()=>expect((screen.getByRole('button',{name:'分析热门文章'}) as HTMLButtonElement).disabled).toBe(false));
  expect(Array.from((screen.getByLabelText('当前 AI 服务') as HTMLSelectElement).options).map(option=>option.value)).toEqual(['','provider-a']);
  fireEvent.click(screen.getByText('检索服务 · 云端向量 + 关键词 · 云端重排'));
  expect(Array.from((await screen.findByLabelText('语义检索服务') as HTMLSelectElement).options).map(option=>option.value)).toEqual(['','embedding-a']);
  expect(Array.from((screen.getByLabelText('重排服务') as HTMLSelectElement).options).map(option=>option.value)).toEqual(['','rerank-a']);
  fireEvent.click(screen.getByRole('button',{name:'分析热门文章'}));await screen.findByText(result.text);
  expect(posted(fetch,'/api/ai/jobs')[0]).toMatchObject({embedding_provider_id:'embedding-a',rerank_provider_id:'rerank-a',provider_id:'provider-a'});
  fireEvent.click(screen.getByRole('tab',{name:'资料聊天'}));fireEvent.change(screen.getByLabelText('向帖子资料提问'),{target:{value:'找类似的表达'}});fireEvent.click(screen.getByRole('button',{name:'发送问题'}));
  await screen.findByText(result.text);expect(posted(fetch,'/api/ai/chat')[0]).toMatchObject({embedding_provider_id:'embedding-a',rerank_provider_id:'rerank-a',provider_id:'provider-a'});
 },30000);
 it('omits unselected retrieval roles and accurately labels keyword-only retrieval',async()=>{
  const {fetch}=setup({configured:true,extraProviders:[{...provider,id:'embedding-a',protocol:'openai_embeddings'},{...provider,id:'rerank-a',protocol:'rerank'}]});
  await waitFor(()=>expect((screen.getByRole('button',{name:'分析热门文章'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByText('检索服务 · 云端向量 + 关键词 · 云端重排'));
  fireEvent.change(await screen.findByLabelText('语义检索服务'),{target:{value:''}});fireEvent.change(screen.getByLabelText('重排服务'),{target:{value:''}});
  expect(screen.getByText('检索服务 · 关键词检索')).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'分析热门文章'}));await screen.findByText(result.text);
  expect(posted(fetch,'/api/ai/jobs')[0]).not.toHaveProperty('embedding_provider_id');expect(posted(fetch,'/api/ai/jobs')[0]).not.toHaveProperty('rerank_provider_id');
 },30000);
 it('rejects a response whose cloud retrieval selection changed while it was running',async()=>{
  let finish:((response:Response)=>void)|undefined;
  setup({configured:true,extraProviders:[{...provider,id:'embedding-a',protocol:'openai_embeddings'}],runResponse:()=>new Promise(resolve=>{finish=resolve;})});
  await waitFor(()=>expect((screen.getByRole('button',{name:'分析热门文章'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByText('检索服务 · 云端向量 + 关键词'));fireEvent.click(screen.getByRole('button',{name:'分析热门文章'}));
  await waitFor(()=>expect((screen.getByRole('button',{name:/分析热门文章/}) as HTMLButtonElement).disabled).toBe(true));
  await waitFor(()=>expect(finish).toBeTypeOf('function'));
  fireEvent.change(screen.getByLabelText('语义检索服务'),{target:{value:''}});finish!(new Response(JSON.stringify(result),{status:200}));
  await waitFor(()=>expect((screen.getByRole('button',{name:'分析热门文章'}) as HTMLButtonElement).disabled).toBe(false));expect(screen.queryByText(result.text)).toBeNull();
 },30000);
 it.each(['openai_embeddings','rerank'])('saves a separate %s service without text-generation options or automatic calls',async protocol=>{
  const {fetch}=setup({settingsPage:true});fireEvent.change(await screen.findByLabelText('服务类型'),{target:{value:protocol}});
  await waitFor(()=>expect(screen.queryByText('高级兼容设置')).toBeNull());
  fireEvent.change(screen.getByLabelText('配置名称'),{target:{value:'独立检索服务'}});fireEvent.change(screen.getByLabelText('模型 ID'),{target:{value:'retrieval-model'}});fireEvent.change(screen.getByLabelText('API Base URL'),{target:{value:'https://retrieval.example.com/v1'}});fireEvent.change(screen.getByLabelText('API Key（本机服务可留空）'),{target:{value:'test-placeholder-key'}});fireEvent.click(screen.getByRole('button',{name:'保存 API 配置'}));
  await waitFor(()=>expect(posted(fetch,'/api/ai/providers/save')).toHaveLength(1));const payload=posted(fetch,'/api/ai/providers/save')[0];
  expect(payload).toMatchObject({protocol,model:'retrieval-model',base_url:'https://retrieval.example.com/v1'});expect(payload).not.toHaveProperty('thinking_mode');expect(payload).not.toHaveProperty('max_output_tokens');expect(payload).not.toHaveProperty('token_parameter');
  expect(posted(fetch,'/api/ai/providers/test')).toHaveLength(0);expect(posted(fetch,'/api/ai/jobs')).toHaveLength(0);
 },30000);

});

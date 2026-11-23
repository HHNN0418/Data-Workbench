// @vitest-environment jsdom
import {afterEach,beforeAll,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
import AISettings from './AISettings';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();vi.unstubAllGlobals();});

it('persists the selected model grade without replacing the saved credential',async()=>{
 const provider={id:'p',name:'Text service',base_url:'http://127.0.0.1:10001/v1',model:'example',protocol:'openai_chat',enabled:true,is_default:true,has_key:true,competition_tier:'standard',max_output_tokens:1800,thinking_mode:'disabled'};
 const settings={providers:[provider],default_provider_id:'p'};
 const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{
  const path=String(input);
  const data=path==='/api/bootstrap'?{csrf_token:'test-token'}:path==='/api/ai/providers/save'?{...settings,providers:[{...provider,...JSON.parse(String(options?.body))}]}:settings;
  return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
 });
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
 render(<QueryClientProvider client={client}><AISettings/></QueryClientProvider>);
 fireEvent.click(await screen.findByRole('button',{name:/编\s*辑/}));
 fireEvent.mouseDown(screen.getByRole('combobox',{name:'模型等级'}));
 fireEvent.click(await screen.findByText('顶级',{selector:'.ant-select-item-option-content'}));
 fireEvent.click(screen.getByRole('button',{name:'保存 API 配置'}));
 await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path==='/api/ai/providers/save')).toBe(true));
 const call=fetch.mock.calls.find(([path])=>path==='/api/ai/providers/save')!;
 const payload=JSON.parse(String(call[1]?.body));
 expect(payload.competition_tier).toBe('premium');
 expect(payload.id).toBe('p');
 expect(payload.max_output_tokens).toBeNull();
 expect(payload.thinking_mode).toBe('auto');
 expect(payload.connection_timeout_seconds).toBe(60);
 expect(screen.queryByLabelText('最大输出 Token 数')).toBeNull();
 expect(screen.queryByText('自动：DeepSeek 关闭思考，其他服务默认')).toBeNull();
 expect(payload).not.toHaveProperty('api_key');
 expect(call[1]?.headers).toMatchObject({'X-CTN-Token':'test-token'});
},30000);

it('shows completion waiting and only reasoning-permitting choices',async()=>{
 const settings={providers:[]};
 vi.stubGlobal('fetch',vi.fn(async()=>new Response(JSON.stringify(settings),{headers:{'Content-Type':'application/json'}})));
 client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
 render(<QueryClientProvider client={client}><AISettings/></QueryClientProvider>);
 fireEvent.click(await screen.findByText('思考与生成'));
 const timeout=screen.getByRole('spinbutton',{name:'连接准备超时（秒）'}) as HTMLInputElement;
 expect(timeout.value).toBe('60');
 expect(timeout.getAttribute('aria-valuemin')).toBe('5');
 expect(timeout.getAttribute('aria-valuemax')).toBe('300');
 expect(screen.getByText('仅限制 DNS 解析、连接、安全握手和发送请求。模型思考与完整回答继续等待，可随时取消。')).toBeTruthy();
 expect(await screen.findByText(/应用不设置生成时长、静默等待或输出 Token 上限/)).toBeTruthy();
 fireEvent.mouseDown(screen.getByRole('combobox',{name:'思考模式'}));
 expect(await screen.findByText('开启思考（服务支持时）',{selector:'.ant-select-item-option-content'})).toBeTruthy();
 expect(screen.getByText('遵循模型默认，允许思考',{selector:'.ant-select-item-option-content'})).toBeTruthy();
 expect(screen.queryByText('关闭思考')).toBeNull();
 expect(screen.queryByLabelText('最大输出 Token 数')).toBeNull();
},30000);

it.each(['openai_chat','rerank','openai_embeddings'])('offers connection preparation controls only for text services: %s',async(protocol)=>{
 const provider={id:'p',name:'Configured service',base_url:'http://127.0.0.1:10001/v1',model:'example',protocol,enabled:true,is_default:true,has_key:true,connection_timeout_seconds:90};
 const settings={providers:[provider],default_provider_id:'p'};
 const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{
  const path=String(input);
  const data=path==='/api/bootstrap'?{csrf_token:'test-token'}:path==='/api/ai/providers/save'?{...settings,providers:[{...provider,...JSON.parse(String(options?.body))}]}:settings;
  return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
 });
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
 render(<QueryClientProvider client={client}><AISettings/></QueryClientProvider>);
 fireEvent.click(await screen.findByRole('button',{name:/编\s*辑/}));
 if(protocol!=='openai_chat'){
  await waitFor(()=>expect(screen.queryByText('思考与生成')).toBeNull());
  expect(screen.queryByRole('spinbutton',{name:'连接准备超时（秒）',hidden:true})).toBeNull();
  expect(screen.queryByText('仅限制 DNS 解析、连接、安全握手和发送请求。模型思考与完整回答继续等待，可随时取消。')).toBeNull();
  return;
 }
 fireEvent.click(await screen.findByText('思考与生成'));
 const timeout=screen.getByRole('spinbutton',{name:'连接准备超时（秒）'}) as HTMLInputElement;
 expect(timeout.value).toBe('90');
 fireEvent.change(timeout,{target:{value:''}});
 fireEvent.click(screen.getByRole('button',{name:'保存 API 配置'}));
 expect(await screen.findByText('请输入 5–300 之间的整数秒数')).toBeTruthy();
 expect(fetch.mock.calls.some(([path])=>path==='/api/ai/providers/save')).toBe(false);
 fireEvent.change(timeout,{target:{value:'120'}});
 fireEvent.click(screen.getByRole('button',{name:'保存 API 配置'}));
 await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path==='/api/ai/providers/save')).toBe(true));
 const call=fetch.mock.calls.find(([path])=>path==='/api/ai/providers/save')!;
 const payload=JSON.parse(String(call[1]?.body));
 expect(payload.connection_timeout_seconds).toBe(120);
 expect(payload.protocol).toBe(protocol);
 expect(payload).not.toHaveProperty('api_key');
},30000);

it('edits the OpenRouter upstream order and clears it for other services',async()=>{
 const provider={id:'p',name:'OpenRouter',base_url:'https://openrouter.ai/api/v1',model:'deepseek/x',protocol:'openai_chat',enabled:true,is_default:true,has_key:true,provider_order:['morph']};
 let settings={providers:[provider],default_provider_id:'p'};
 // A save is kept, as on the server: the page refetches the list right after saving.
 const fetch=vi.fn(async(input:RequestInfo|URL,options?:RequestInit)=>{
  const path=String(input);
  if(path==='/api/ai/providers/save')settings={...settings,providers:[{...provider,...JSON.parse(String(options?.body))}]};
  const data=path==='/api/bootstrap'?{csrf_token:'test-token'}:settings;
  return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
 });
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
 render(<QueryClientProvider client={client}><AISettings/></QueryClientProvider>);
 expect(await screen.findByText('上游顺序：morph')).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:/编\s*辑/}));
 const order=await screen.findByRole('combobox',{name:'上游提供商顺序'});
 fireEvent.mouseDown(order);
 fireEvent.click(await screen.findByText('relace · 便宜',{selector:'.ant-select-item-option-content'}));
 fireEvent.click(screen.getByRole('button',{name:'保存 API 配置'}));
 await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path==='/api/ai/providers/save')).toBe(true));
 const saved=JSON.parse(String(fetch.mock.calls.find(([path])=>path==='/api/ai/providers/save')![1]?.body));
 expect(saved.provider_order).toEqual(['morph','relace']);
 expect(await screen.findByText('上游顺序：morph → relace')).toBeTruthy();
 // Another service never shows the field and saves an empty order.
 fireEvent.click(screen.getByRole('button',{name:/编\s*辑/}));
 fireEvent.change(screen.getByPlaceholderText('https://api.example.com/v1'),{target:{value:'https://api.example.com/v1'}});
 await waitFor(()=>expect(screen.queryByRole('combobox',{name:'上游提供商顺序'})).toBeNull());
 fetch.mockClear();
 fireEvent.click(screen.getByRole('button',{name:'保存 API 配置'}));
 await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path==='/api/ai/providers/save')).toBe(true));
 expect(JSON.parse(String(fetch.mock.calls.find(([path])=>path==='/api/ai/providers/save')![1]?.body)).provider_order).toEqual([]);
},30000);

it('deletes a connection and its saved key only after confirmation',async()=>{
 const provider={id:'p',name:'Text service',base_url:'https://example.test/v1',model:'example',protocol:'openai_chat',enabled:true,is_default:true,has_key:true};
 const fetch=vi.fn(async(input:RequestInfo|URL)=>{
  const path=String(input);
  const data=path==='/api/bootstrap'?{csrf_token:'test-token'}:path==='/api/ai/providers/delete'?{providers:[],default_provider_id:null}:{providers:[provider],default_provider_id:'p'};
  return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
 });
 vi.stubGlobal('fetch',fetch);
 client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
 render(<QueryClientProvider client={client}><AISettings/></QueryClientProvider>);
 fireEvent.click(await screen.findByRole('button',{name:/删\s*除/}));
 expect(await screen.findByText('删除连接「Text service」？')).toBeTruthy();
 expect(screen.getByText(/会一起删除服务端保存的密钥/)).toBeTruthy();
 expect(fetch.mock.calls.some(([path])=>path==='/api/ai/providers/delete')).toBe(false);
 const buttons=screen.getAllByRole('button',{name:/删\s*除/});
 fireEvent.click(buttons[buttons.length-1]);
 await waitFor(()=>expect(fetch.mock.calls.some(([path])=>path==='/api/ai/providers/delete')).toBe(true));
 const call=fetch.mock.calls.find(([path])=>path==='/api/ai/providers/delete')! as unknown as [string,RequestInit];
 expect(JSON.parse(String(call[1].body))).toEqual({id:'p'});
},30000);

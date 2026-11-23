// @vitest-environment jsdom
import {afterEach,expect,it,vi} from 'vitest';
import {act,cleanup,renderHook,waitFor} from '@testing-library/react';
import {useAIJob} from './useAIJob';
afterEach(()=>{cleanup();sessionStorage.clear();vi.useRealTimers();vi.unstubAllGlobals();});
const reply=(data:unknown,status=200)=>Promise.resolve(new Response(JSON.stringify(data),{status}));
const running={id:'job-one',kind:'analysis',scope:'',state:'running',stage:'thinking',received_bytes:2048,created_at:'2026-01-01T00:00:00Z',result:null};
function setup(status:(options?:RequestInit)=>Promise<Response>,start?:()=>Promise<Response>,items:unknown[]=[]){
 let cancelled=false;
 const fetch=vi.fn((input:RequestInfo|URL,options?:RequestInit)=>{
  const path=String(input);
  if(path==='/api/bootstrap')return reply({csrf_token:'test'});
  if(path.startsWith('/api/ai/jobs?'))return reply({items});
  if(path==='/api/ai/jobs')return start?start():reply(running,202);
  if(path.endsWith('/cancel')){cancelled=true;return reply({...running,state:'cancelling'});}
  if(cancelled)return reply({...running,state:'cancelled',error:'AI 任务已取消'});
  return status(options);
 });
 vi.stubGlobal('fetch',fetch);
 return {...renderHook(()=>useAIJob<{text:string}>()),fetch};
}
it('polls status and returns an article without a synchronous model request',async()=>{
 const {result,fetch}=setup(()=>reply({...running,state:'completed',result:{text:'完整文章'}}));
 let promise!:Promise<{text:string}>;
 act(()=>{promise=result.current.mutateAsync({mode:'write'});});
 await waitFor(()=>expect(result.current.stage).toContain('思考'));
 await act(async()=>{expect(await promise).toEqual({text:'完整文章'});});
 expect(fetch.mock.calls.some(call=>String(call[0])==='/api/ai/run')).toBe(false);
});
it('manual cancellation stops work and discards its result',async()=>{
 const {result,fetch}=setup(()=>reply(running));let promise!:Promise<string>;
 act(()=>{promise=result.current.mutateAsync({}).then(()=>'',error=>error.message);});
 await waitFor(()=>expect(result.current.stage).toContain('思考'));
 await act(async()=>{await result.current.cancel();expect(await promise).toBe('AI 任务已取消');});
 expect(fetch.mock.calls.some(call=>String(call[0]).endsWith('/cancel'))).toBe(true);
});
it('leaving the page keeps the owned backend job running',async()=>{
 const {result,fetch,unmount}=setup(()=>reply(running));let promise!:Promise<unknown>;
 act(()=>{promise=result.current.mutateAsync({}).catch(error=>error);});
 await waitFor(()=>expect(result.current.stage).toContain('思考'));unmount();await promise;
 expect(fetch.mock.calls.some(call=>String(call[0]).endsWith('/cancel'))).toBe(false);
});
it('prevents duplicate submissions even in the same render',async()=>{
 let finish!:(response:Response)=>void;
 const {result,fetch}=setup(()=>reply(running),()=>new Promise(resolve=>{finish=resolve;}));
 let first!:Promise<unknown>,second!:Promise<string>;
 act(()=>{first=result.current.mutateAsync({});second=result.current.mutateAsync({}).then(()=>'',error=>error.message);});
 expect(await second).toContain('已有 AI 任务');
 await waitFor(()=>expect(finish).toBeTypeOf('function'));
 await act(async()=>{finish(new Response(JSON.stringify({...running,state:'completed',result:{text:'done'}})));expect(await first).toEqual({text:'done'});});
 expect(fetch.mock.calls.filter(call=>String(call[0])==='/api/ai/jobs')).toHaveLength(1);
});
it('restores a running task and its eventual result after reopening the page',async()=>{
 const {result}=setup(()=>reply({...running,state:'completed',result:{text:'恢复的结果'}}),undefined,[running]);
 await waitFor(()=>expect(result.current.stage).toContain('思考'));
 await waitFor(()=>expect(result.current.recoveredResult).toEqual({text:'恢复的结果'}),{timeout:3000});
});
it('a failed status connection never cancels or resubmits the model',async()=>{
 let reads=0;
 const {result,fetch}=setup(()=>++reads===1?Promise.reject(new Error('temporary network loss')):reply({...running,state:'completed',result:{text:'done'}}));
 let promise!:Promise<unknown>;act(()=>{promise=result.current.mutateAsync({});});
 await act(async()=>{expect(await promise).toEqual({text:'done'});});
 expect(fetch.mock.calls.some(call=>String(call[0]).endsWith('/cancel'))).toBe(false);
 expect(fetch.mock.calls.filter(call=>String(call[0])==='/api/ai/jobs')).toHaveLength(1);
});

it('allows a completed result body to take more than ten seconds to download',async()=>{
 vi.useFakeTimers();
 let signal:AbortSignal|undefined;
 const {result,fetch}=setup(options=>{
  signal=options?.signal??undefined;
  return Promise.resolve({ok:true,status:200,json:()=>new Promise((resolve,reject)=>{
   const timer=window.setTimeout(()=>resolve({...running,state:'completed',result:{text:'完整的大报告'}}),11000);
   signal?.addEventListener('abort',()=>{window.clearTimeout(timer);reject(new DOMException('Aborted','AbortError'));},{once:true});
  })} as Response);
 });
 let promise!:Promise<{text:string}>;
 act(()=>{promise=result.current.mutateAsync({mode:'popular'});});
 await act(async()=>{await vi.advanceTimersByTimeAsync(1000);});
 expect(signal).toBeDefined();
 await act(async()=>{await vi.advanceTimersByTimeAsync(10001);});
 expect(signal?.aborted).toBe(false);
 expect(result.current.isPending).toBe(true);
 await act(async()=>{await vi.advanceTimersByTimeAsync(999);expect(await promise).toEqual({text:'完整的大报告'});});
 expect(fetch.mock.calls.filter(call=>String(call[0])==='/api/ai/jobs')).toHaveLength(1);
 expect(fetch.mock.calls.filter(call=>String(call[0])==='/api/ai/jobs/job-one')).toHaveLength(1);
});

it('manual cancellation aborts a pending result download without waiting for its body',async()=>{
 vi.useFakeTimers();
 let signal:AbortSignal|undefined,readBody=false;
 const {result,fetch}=setup(options=>{
  signal=options?.signal??undefined;
  return Promise.resolve({ok:true,status:200,json:()=>{
   readBody=true;
   return new Promise((_,reject)=>signal?.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true}));
  }} as Response);
 });
 let promise!:Promise<string>;
 act(()=>{promise=result.current.mutateAsync({}).then(()=>'',failure=>failure.message);});
 await act(async()=>{await vi.advanceTimersByTimeAsync(1000);});
 expect(readBody).toBe(true);
 await act(async()=>{await result.current.cancel();expect(await promise).toBe('AI 任务已取消');});
 expect(signal?.aborted).toBe(true);
 expect(result.current.isPending).toBe(false);
 expect(fetch.mock.calls.filter(call=>String(call[0]).endsWith('/cancel'))).toHaveLength(1);
 expect(fetch.mock.calls.filter(call=>String(call[0])==='/api/ai/jobs')).toHaveLength(1);
});

it('restores the full result of a completed job listed without its result body',async()=>{
 const {result}=setup(()=>reply({...running,state:'completed',result:{text:'完整恢复结果'}}),undefined,
  [{...running,state:'completed',result:undefined}]);
 sessionStorage.setItem('ctn-ai-job:analysis:','job-one');
 await waitFor(()=>expect(result.current.recoveredResult).toEqual({text:'完整恢复结果'}),{timeout:3000});
});

it('clears completed results and request variables when switching role scopes',async()=>{
 vi.stubGlobal('fetch',vi.fn((input:RequestInfo|URL)=>{
  const path=String(input);
  if(path==='/api/bootstrap')return reply({csrf_token:'test'});
  if(path.startsWith('/api/ai/jobs?'))return reply({items:[]});
  return reply({...running,kind:'persona',scope:'role-a',state:'completed',result:{text:'角色 A 的人设'}},202);
 }));
 const {result,rerender}=renderHook(({scope})=>useAIJob<{text:string}>({
  path:'/agents/generate-persona',kind:'persona',scopeKey:scope,
 }),{initialProps:{scope:'role-a'}});
 await act(async()=>{await result.current.mutateAsync({existing_agent_id:'role-a'});});
 expect(result.current.data).toEqual({text:'角色 A 的人设'});
 expect(result.current.variables).toEqual({existing_agent_id:'role-a'});
 rerender({scope:'role-b'});
 expect(result.current.data).toBeUndefined();
 expect(result.current.variables).toBeUndefined();
 expect(result.current.recoveredResult).toBeUndefined();
 expect(result.current.jobId).toBeUndefined();
 expect(result.current.isPending).toBe(false);
});

it('does not rerender the owning page every second when there is no active job',async()=>{
 vi.useFakeTimers();
 vi.stubGlobal('fetch',vi.fn(()=>reply({items:[]})));
 let renders=0;
 renderHook(()=>{renders++;return useAIJob<{text:string}>();});
 await act(async()=>{await vi.advanceTimersByTimeAsync(1);});
 const idleRenders=renders;
 for(let second=0;second<10;second++)await act(async()=>{await vi.advanceTimersByTimeAsync(1000);});
 expect(renders).toBe(idleRenders);
});

it('freezes the completed elapsed time and releases the live clock',async()=>{
 vi.useFakeTimers();
 const {result}=setup(()=>reply({...running,state:'completed',result:{text:'done'}}));
 let promise!:Promise<{text:string}>;
 act(()=>{promise=result.current.mutateAsync({});});
 await act(async()=>{await vi.advanceTimersByTimeAsync(1000);expect(await promise).toEqual({text:'done'});});
 expect(result.current.isPending).toBe(false);
 const completed=result.current,elapsed=result.current.elapsedSeconds;
 for(let second=0;second<5;second++)await act(async()=>{await vi.advanceTimersByTimeAsync(1000);});
 expect(result.current.elapsedSeconds).toBe(elapsed);
 expect(result.current).toBe(completed);
});

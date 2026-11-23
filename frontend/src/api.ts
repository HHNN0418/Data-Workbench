import {hashKey, useQuery, useMutation, useQueryClient} from '@tanstack/react-query';
export type Row = Record<string, any>;
export interface PageResult {items: Row[]; total:number; limit:number; offset:number; [key:string]:any}
export function queryString(params:Record<string,unknown> = {}) {const q=new URLSearchParams(); Object.entries(params).forEach(([k,v])=>{if(v!==''&&v!==undefined&&v!==null)q.set(k,String(v))});return q.toString()}
export async function request<T = Row>(path:string, options:RequestInit = {}):Promise<T> {
 const response=await fetch('/api'+path,options);
 if(response.status===401&&!path.startsWith('/auth/')&&!options.signal?.aborted)window.dispatchEvent(new Event('ctn:unauthorized'));
 // A proxy or tunnel error page is not JSON; report its status, not a parse error.
 let data:any;
 try{data=await response.json();}catch(error){if(!response.ok)throw new Error(`请求失败 (${response.status})`);throw error;}
 if(!response.ok)throw new Error(data?.error || data?.detail || `请求失败 (${response.status})`); return data;
}
export function useApi<T=Row>(path:string, params:Record<string,unknown>={}, interval?:number|false, retainPrevious=false) {
 const analysis=path==='/analysis';
 // Reuse recent analysis; poll for new data every five minutes. A full
 // analysis takes about a minute on live data, so minute polling recomputed it
 // almost continuously. Reopening after the one-minute TTL still recomputes.
 // Let an initial analysis finish after navigation so returning can reuse it.
 return useQuery<T,Error>({queryKey:[path,params],queryFn:context=>request<T>(path+(queryString(params)?'?'+queryString(params):''),analysis?{}:{signal:context.signal}),placeholderData:retainPrevious?(previous=>previous):undefined,...(analysis?{staleTime:60000,gcTime:30*60*1000,refetchInterval:interval??300000,refetchOnWindowFocus:true,refetchOnReconnect:true}:{refetchInterval:interval??15000,refetchOnWindowFocus:true})});
}
const analysisViewParameters=new Set(['sections','include_exportable','focus_post_id','sample_stage','sample_status','sample_batch','detail_limit','cohort_limit']);
function analysisScope(params:Row) {return hashKey([Object.fromEntries(Object.entries(params).filter(([key])=>!analysisViewParameters.has(key)))]);}
export function useAnalysisRefresh() {
 const client=useQueryClient();
 const matchesScope=(queryKey:readonly unknown[],params:Row)=>queryKey[0]==='/analysis'&&analysisScope((queryKey[1]||{}) as Row)===analysisScope(params);
 return useMutation<Row,Error,Row>({mutationFn:async params=>{
  await client.cancelQueries({predicate:query=>matchesScope(query.queryKey,params)});
  return request<Row>('/analysis?'+queryString({...params,refresh:true}));
 },onSuccess:async(data,params)=>{
  // A tab opened during the refresh may still be reading the older result.
  // Cancel it before seeding the refreshed result, then reload visible siblings.
  await client.cancelQueries({predicate:query=>matchesScope(query.queryKey,params)});
  client.setQueryData(['/analysis',params],data);
  const target=hashKey(['/analysis',params]);
  await client.invalidateQueries({predicate:query=>matchesScope(query.queryKey,params)&&hashKey(query.queryKey)!==target,refetchType:'active'});
 }});
}
export function useLocalDataRefresh() {
 const client=useQueryClient(),analysis=useAnalysisRefresh();
 return useMutation<void,Error,Row|undefined>({mutationFn:async params=>{
  if(params){
   await analysis.mutateAsync(params);
   await client.invalidateQueries({predicate:query=>query.queryKey[0]!=='/analysis'});
  }else await client.invalidateQueries();
 }});
}
export function useAction<T=Row>(path:string) {
 const client=useQueryClient();
 return useMutation<T,Error,Row>({mutationFn:async payload=>{const b=await client.fetchQuery({queryKey:['bootstrap-token'],queryFn:()=>request('/bootstrap'),staleTime:0});return request<T>(path,{method:'POST',headers:{'Content-Type':'application/json','X-CTN-Token':b.csrf_token},body:JSON.stringify(payload)})},onSuccess:(_data,payload)=>{
  if(path==='/agents/reset-memory')client.removeQueries({predicate:query=>query.queryKey[0]==='/agents/memory-history'&&(query.queryKey[1] as Row)?.agent_id===payload.agent_id});
  if(path.startsWith('/agents')){void client.invalidateQueries({queryKey:['/agents']});return;}
  if(path==='/analysis/run')return;
  // AI reads and model calls do not change collected data or analysis results.
  if(['/ai/preview','/ai/chat','/ai/run','/ai/providers/test','/ai/account-screening','/ai/account-review'].includes(path))return;
  if(['/ai/providers/save','/ai/providers/delete'].includes(path)){void client.invalidateQueries({queryKey:['/ai/providers']});return;}
  void client.invalidateQueries();
 }});
}

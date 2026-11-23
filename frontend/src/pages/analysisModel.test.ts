import {describe, expect, it} from 'vitest';
import {analysisFullRequestParams, analysisRequestParams, positiveId, readAnalysisParams, replyTreeOption, samplingOption, trajectoryOption, updateAnalysisSearch, visibleReplyNodes, visibleTrajectories} from './analysisModel';

describe('analysis model',()=>{
 it('preserves batch, status and focus when saving or exporting the displayed scope',()=>{
  const initial=new URLSearchParams('analysis_tab=deep&deep_view=coverage&sample_batch=2026-10-19T00%3A00%3A00Z&sample_status=eligible&focus_post_id=7&own=own');
  const updated=readAnalysisParams(updateAnalysisSearch(initial,{window_minutes:30,tolerance_minutes:6}));
  const full=analysisFullRequestParams(updated),shown=analysisRequestParams(updated);
  expect(full).toEqual({...shown,sections:'all',cohort_limit:10000});
  expect(full).toMatchObject({sample_batch:'2026-10-19T00:00:00Z',sample_status:'eligible',focus_post_id:7,own:'own',window_minutes:30});
  expect(full).not.toHaveProperty('deep_view');
 });
 it('requests only the current details without narrowing aggregate scope',()=>{
  const scope={own:'own',author_id:'42',window_minutes:60,detail_limit:1000,focus_post_id:8};
  expect(analysisRequestParams(scope)).toEqual({...scope,sections:'core',include_exportable:false});
  for(const [tab,view,section] of [['growth','ranks','growth'],['network','ranks','network'],['deep','ranks','trending'],['deep','replies','replies'],['deep','coverage','sampling']]) {
   expect(analysisRequestParams({...scope,analysis_tab:tab,deep_view:view,rank_sort:'peak'})).toEqual({...scope,sections:section,include_exportable:false});
  }
 });
 it('explicitly hides dense tree labels so ECharts cannot enable them by default',()=>{
  const nodes=Array.from({length:40},(_,index)=>({post_id:index+1,parent_id:index?1:null,depth:index?1:0}));
  const option=replyTreeOption(nodes,3) as any;
  const root=option.series[0].data[0];
  expect(root.label.show).toBe(true);
  expect(root.children.filter((node:any)=>node.label.show)).toHaveLength(1);
  expect(root.children.every((node:any)=>typeof node.label.show==='boolean')).toBe(true);
 });

 it('keeps empty numeric URL values as defaults and validates the shared focus',()=>{
  expect(readAnalysisParams(new URLSearchParams('window_minutes=&focus_post_id=&sample_status=bad'))).toMatchObject({window_minutes:60});
  expect(readAnalysisParams(new URLSearchParams('focus_post_id=12&sample_status=eligible&sample_stage=early_baseline'))).toMatchObject({focus_post_id:12,sample_status:'eligible'});
  expect(readAnalysisParams(new URLSearchParams('sample_stage=early_baseline'))).toMatchObject({sample_stage:'early_baseline'});
  expect(positiveId('')).toBeNull();expect(positiveId('-1')).toBeNull();expect(positiveId('1.5')).toBeNull();
 });
 it('preserves scope while changing and clearing cross-chart selection',()=>{
  const original=new URLSearchParams('own=own&window_minutes=30&sample_status=eligible&focus_post_id=1');
  const next=updateAnalysisSearch(original,{focus_post_id:2,sample_stage:'early_baseline',sample_status:null});
  expect(next.get('own')).toBe('own');expect(next.get('window_minutes')).toBe('30');expect(next.get('focus_post_id')).toBe('2');expect(next.has('sample_status')).toBe(false);
  expect(original.get('focus_post_id')).toBe('1');
 });
 it('limits trajectory clutter while always retaining the chosen post',()=>{
  const rows=Array.from({length:20},(_,i)=>({post_id:i+1}));
  expect(visibleTrajectories(rows,20)).toHaveLength(5);expect(visibleTrajectories(rows,20).at(-1)?.post_id).toBe(20);
 });
 it('leaves rank gaps null on a common real-time axis and never interpolates missing rounds',()=>{
  const option=trajectoryOption([{post_id:1,username:'A',points:[{observed_at:'2026-10-19T00:00:00Z',rank:5},{observed_at:'2026-10-19T00:10:00Z',rank:2}]},{post_id:2,username:'B',points:[{observed_at:'2026-10-19T00:05:00Z',rank:8}]}],1) as any;
  expect(option.xAxis.type).toBe('time');expect(option.yAxis.inverse).toBe(true);expect(option.series[0].connectNulls).toBe(false);
  expect(option.series[0].data.map((row:any)=>row.value[1])).toEqual([5,null,2]);expect(option.series[1].data.map((row:any)=>row.value[1])).toEqual([null,8,null]);
  expect(option.series[0].data[0].post_id).toBe(1);expect(option.tooltip.renderMode).toBe('richText');
 });
 it('bounds the reply drawing and retains the selected node and its ancestors',()=>{
  const nodes=Array.from({length:150},(_,i)=>({post_id:i+1,parent_id:i>0?Math.floor((i-1)/2)+1:null,depth:i>0?Math.floor(Math.log2(i+1)):0}));
  const visible=visibleReplyNodes(nodes,150,12);expect(visible.length).toBeLessThanOrEqual(12);
  const ids=new Set(visible.map(row=>row.post_id));let node=nodes[149];while(node){expect(ids.has(node.post_id)).toBe(true);node=nodes.find(row=>row.post_id===node.parent_id)!;}
 });
 it('breaks long observation gaps even for a single trajectory',()=>{
  const option=trajectoryOption([{post_id:1,points:[{observed_at:'2026-10-19T00:00:00Z',rank:5},{observed_at:'2026-10-19T03:00:00Z',rank:2}]}],null) as any;
  expect(option.series[0].data.map((row:any)=>row.value[1])).toEqual([5,null,2]);
 });
 it('bounds plotted data by visible observations without bridging missing rounds from hidden posts',()=>{
  const start=Date.parse('2026-10-19T00:00:00Z');
  const rows=Array.from({length:1000},(_,r)=>({post_id:r+1,points:Array.from({length:100},(_,p)=>({observed_at:new Date(start+(p*1000+r)*1000).toISOString(),rank:r+1}))}));
  const option=trajectoryOption(rows.slice(0,5),null,'UTC',rows) as any;
  expect(option.series.reduce((sum:number,series:any)=>sum+series.data.length,0)).toBeLessThanOrEqual(1005);
  for(const series of option.series){
   expect(series.data.filter((point:any)=>point.value[1]!==null)).toHaveLength(100);
   expect(series.data.every((point:any,index:number)=>index===0||point.value[1]===null||series.data[index-1].value[1]===null)).toBe(true);
  }
 });
 it('connects adjacent observed rounds and ignores invalid timestamps',()=>{
  const option=trajectoryOption([{post_id:1,points:[{observed_at:'bad',rank:9},{observed_at:'2026-10-19T00:01:00Z',rank:3},{observed_at:'2026-10-19T00:00:00Z',rank:5}]}],null) as any;
  expect(option.series[0].data.map((point:any)=>point.value[1])).toEqual([5,3]);
 });
 it('keeps root and focus in a very deep chain with an explicit omitted-level node',()=>{
  const nodes=Array.from({length:3000},(_,index)=>({post_id:index+1,parent_id:index||null,depth:index}));
  const visible=visibleReplyNodes(nodes,3000);
  expect(visible).toHaveLength(40);expect(visible.some(node=>node.post_id===1)).toBe(true);expect(visible.some(node=>node.post_id===3000)).toBe(true);
  expect(visible.find(node=>node.virtual)?.omitted).toBe(2961);
 });
 it('does not recurse forever on bad reply relations or render raw HTML tooltips',()=>{
  const option=replyTreeOption([{post_id:1,parent_id:2,body_preview:'<img src=x onerror=alert(1)>'},{post_id:2,parent_id:1}],1) as any;
  expect(option.series[0].data[0].virtual).toBe(true);expect(option.tooltip.renderMode).toBe('richText');
  expect(option.tooltip.formatter({data:{post_id:1,body_preview:'<img>'}})).toContain('<img>');
 });
 it('binds passed bars to stages and loss bars to their actual exclusive reason',()=>{
  const option=samplingOption([{key:'total',label:'全部',count:9},{key:'early_baseline',label:'早期基线',count:3}],[{key:'no_early_baseline',count:4},{key:'missing_created_at',count:2}]) as any;
  expect(option.series[0].data[1]).toMatchObject({value:3,stage:'early_baseline'});
  const reasons=option.series.slice(1).map((series:any)=>series.data[1]);
  expect(reasons).toContainEqual({value:4,label:'缺早期基线',status:'no_early_baseline'});expect(reasons).toContainEqual({value:2,label:'缺发布时间',status:'missing_created_at'});
 });
});


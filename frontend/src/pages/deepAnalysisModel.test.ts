import {describe,it,expect} from 'vitest';
import {rankedRows,rankHeatmap,coverageHeatmap,branchNodes,replyStats,interactionOption} from './deepAnalysisModel';
describe('deep analysis evidence',()=>{
 it('ranks improvements ahead of single observations and retains the selected post',()=>{
  const rows=[{post_id:1,first_rank:8,last_rank:2,best_rank:2,observations:2},{post_id:2,first_rank:1,last_rank:1,best_rank:1,observations:1},{post_id:3,first_rank:9,last_rank:8,best_rank:8,observations:4}];
  expect(rankedRows(rows,'rise',null).map(r=>r.post_id)).toEqual([1,3,2]);
  expect(rankedRows(rows,'peak',3,1)[0].post_id).toBe(3);
 });
 it('leaves unobserved heatmap cells empty and aggregates only actual ranks',()=>{
  const chart=rankHeatmap([{post_id:1,points:[{observed_at:'2026-10-19T00:00:00Z',rank:8},{observed_at:'2026-10-19T00:00:20Z',rank:3},{observed_at:'2026-10-19T00:05:00Z',rank:5}]}],'UTC');
  expect(chart.series[0].data).toHaveLength(2);
  expect(chart.series[0].data[0]).toMatchObject({value:[0,0,3],observations:2});
 });
 it('distinguishes no mature samples from zero coverage',()=>{
  const chart=coverageHeatmap([{batch:'2026-10-19T00:00:00Z',minutes:30,rate:null},{batch:'2026-10-19T00:00:00Z',minutes:60,rate:0}],'UTC');
  expect(chart.series[0].data.map((r:any)=>r.value[2])).toEqual([-1,0]);
 });
 it('isolates descendants and calculates branching without including sibling conversations',()=>{
  const nodes=[{post_id:1,depth:0,author_id:1},{post_id:2,parent_id:1,depth:1,author_id:2},{post_id:3,parent_id:2,depth:2,author_id:2},{post_id:4,parent_id:1,depth:1,author_id:3}];
  const stats=replyStats(nodes);
  expect(stats).toMatchObject({participants:3,largest_branch:2,reply_count:3,max_depth:2});
  const branch=branchNodes(nodes,2);
  expect(branch.map(r=>r.post_id)).toEqual([2,3]);
  expect(replyStats(branch)).toMatchObject({max_depth:1,participants:1});
 });
 it('does not convert missing or decreasing counts into positive growth',()=>{
  const chart=interactionOption([{observed_at:'2026-10-19T00:00:00Z',like_count:10},{observed_at:'2026-10-19T00:01:00Z',like_count:9},{observed_at:'2026-10-19T00:02:00Z',like_count:null},{observed_at:'2026-10-19T00:03:00Z',like_count:12}],'UTC');
  expect(chart.series[0].data.map((r:any)=>r[1])).toEqual([null,null,null,null]);
 });
});

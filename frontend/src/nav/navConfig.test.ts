import {describe, expect, it} from 'vitest';
import {breadcrumbs, matchNav, navGroups, navItems, pageTitle, tabLabel} from './navConfig';

const split=(to:string)=>{const [path,query='']=to.split('?');return [path,query] as const;};

describe('navigation config',()=>{
 it('follows the data flow from collection to results, with system last',()=>{
  expect(navGroups.map(group=>group.key)).toEqual(['overview','collect','process','analyze','strategy','execute','results','system']);
  expect(navGroups.filter(group=>group.collapsible).map(group=>group.key)).toEqual(['system']);
  expect(navItems[0].to).toBe('/');
  const order=navGroups.map(group=>group.key);
  expect(navItems.map(item=>order.indexOf(item.group))).toEqual([...navItems.map(item=>order.indexOf(item.group))].sort((a,b)=>a-b));
 });

 it('puts every item in a known group and gives each page exactly one entry',()=>{
  const groups=new Set(navGroups.map(group=>group.key));
  expect(navItems.every(item=>groups.has(item.group))).toBe(true);
  expect(new Set(navItems.map(item=>item.key)).size).toBe(navItems.length);
  expect(new Set(navItems.map(item=>item.to)).size).toBe(navItems.length);
 });

 it('highlights each item when its own link is opened',()=>{
  for(const item of navItems.filter(item=>!item.external))expect(matchNav(...split(item.to))?.key,item.to).toBe(item.key);
 });

 it.each([
  ['/','','cockpit'],
  ['/ai/analysis','','radar'],
  ['/ai/analysis','ai_view=research&own=own','radar'],
  ['/ai/analysis','ai_view=chat','workbench'],
  ['/ai/analysis','ai_view=screening','blue'],
  ['/ai/facts','','facts'],
  ['/ai/facts','tab=active','facts'],
  ['/ai/facts','tab=extraction','extraction'],
  ['/ai/facts','tab=prompts','prompts'],
  ['/ai/agents','','roles'],
  ['/ai/agents','tab=logs','roles'],
  ['/ai/agents','tab=drafts&draft=d-1','pending'],
  ['/ai/agents','tab=inbox','pending'],
  ['/ai/agents','tab=quotas','quotas'],
  // A role's own records and manual panel belong to that role.
  ['/ai/agents/maya','tab=drafts&draft=d-1','roles'],
  ['/ai/agents/maya','tab=manual','roles'],
  ['/agents/pipeline','','pipeline'],
  ['/processing/bots','','bots'],
  ['/analysis/clusters','blue_cluster=c5','clusters'],
  ['/','view=data','cockpit'],
  ['/posts','post=12&feed=trending','posts'],
  ['/operations/','','operations']
 ])('matches %s?%s to %s',(path,query,key)=>{
  expect(matchNav(path,query)?.key).toBe(key);
 });

 it('has no entry for unknown paths',()=>{
  expect(matchNav('/missing')).toBeUndefined();
  expect(breadcrumbs('/missing')).toEqual([]);
 });

 it('builds stage, page, entity and tab crumbs with links on all but the last',()=>{
  expect(breadcrumbs('/ai/agents/maya','tab=memory',id=>id==='maya'?'Maya':undefined)).toEqual([
   {label:'Agent 执行'},{label:'角色',to:'/ai/agents'},{label:'Maya',to:'/ai/agents/maya'},{label:'经历记忆'}
  ]);
  expect(breadcrumbs('/ai/agents','tab=drafts')).toEqual([{label:'Agent 执行'},{label:'待处理',to:'/ai/agents?tab=inbox'},{label:'操作记录'}]);
  expect(breadcrumbs('/news','news_view=tags')).toEqual([{label:'采集'},{label:'新闻与话题',to:'/news'},{label:'话题标签'}]);
  expect(breadcrumbs('/processing')).toEqual([{label:'处理'},{label:'标注'}]);
  expect(breadcrumbs('/')).toEqual([{label:'全链路总览'}]);
  expect(breadcrumbs('/','view=data')).toEqual([{label:'全链路总览',to:'/'},{label:'数据总览'}]);
  expect(breadcrumbs('/analysis/clusters')).toEqual([{label:'分析'},{label:'账号簇 / 竞品'}]);
 });

 it('links the post crumb back to the filtered list',()=>{
  expect(breadcrumbs('/posts','feed=trending&q=vote&post=42')).toEqual([{label:'采集'},{label:'帖子流',to:'/posts?feed=trending&q=vote'},{label:'#42'}]);
 });

 it('reads tab labels only for tabs it knows',()=>{
  expect(tabLabel('/posts','feed=all')).toBe('全部已采集');
  expect(tabLabel('/posts','feed=other')).toBeUndefined();
  expect(tabLabel('/ai/agents/maya','tab=manual')).toBe('手动操作');
 });

 it('leaves the title to pages that still render their own heading',()=>{
  expect(pageTitle('/')).toBe('全链路总览');
  expect(pageTitle('/score')).toBeUndefined();
  expect(pageTitle('/operations')).toBeUndefined();
  expect(pageTitle('/posts')).toBe('帖子流');
  expect(pageTitle('/strategy')).toBe('目标与配比规则');
  expect(pageTitle('/ai/agents','tab=quotas')).toBe('额度');
 });
});

// @vitest-environment jsdom
import {afterEach, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {MemoryRouter, Route, Routes, useLocation, useNavigate} from 'react-router-dom';
import AppRoutes from './routes';
import {navItems} from './nav/navConfig';

vi.mock('./pages/PipelineHome',()=>({default:()=><p>全链路内容</p>}));
vi.mock('./pages/Records',()=>({Posts:()=><p>帖子内容</p>,Accounts:()=><p>账号内容</p>,News:()=><p>新闻内容</p>}));
vi.mock('./pages/Score',()=>({default:()=><p>积分内容</p>}));
vi.mock('./pages/Analysis',()=>({default:()=><p>分析内容</p>}));
vi.mock('./pages/AIContentAnalysis',()=>({default:()=><p>内容研究</p>}));
vi.mock('./pages/Operations',()=>({default:()=><p>运行内容</p>}));
vi.mock('./pages/Rules',()=>({default:()=><p>规则内容</p>}));
vi.mock('./pages/Agents',()=>({default:()=><p>角色内容</p>}));
vi.mock('./pages/AISettings',()=>({default:()=><p>设置内容</p>}));
vi.mock('./pages/AIReview',()=>({default:()=><p>审核内容</p>}));
vi.mock('./pages/FactBank',()=>({default:()=><p>事实内容</p>}));
vi.mock('./pages/ReadingSelection',()=>({default:()=><p>筛选内容</p>}));
vi.mock('./pages/BlueCandidates',()=>({default:()=><p>举报候选</p>}));
vi.mock('./pages/AccountClusters',()=>({default:()=><p>账号簇内容</p>}));
vi.mock('./pages/Leaderboard',()=>({default:()=><p>排行榜内容</p>}));
vi.mock('./pages/AccountIdentity',()=>({default:()=><p>账号识别内容</p>}));
vi.mock('./pages/RiskAssessment',()=>({default:()=><p>风险评估内容</p>}));
vi.mock('./pages/Processing',()=>({default:()=><p>标注内容</p>}));
vi.mock('./pages/Strategy',()=>({default:()=><p>规则内容（策略）</p>}));
vi.mock('./pages/AgentPipeline',()=>({default:()=><p>流水线内容</p>}));
vi.mock('./pages/Attribution',()=>({default:()=><p>归因内容</p>}));
vi.mock('./pages/Exposure',()=>({default:()=><p>曝光内容</p>}));
vi.mock('./pages/SettingsIndex',()=>({default:()=><p>设置索引</p>}));
vi.mock('./pages/Maintenance',()=>({default:()=><p>维护内容</p>}));

afterEach(cleanup);

function Location() {
 const location=useLocation(),navigate=useNavigate();
 return <><output data-testid="location">{location.pathname}{location.search}{location.hash}</output><button onClick={()=>navigate(-1)}>返回上一页</button></>;
}

function setup(initial:string,previous?:string) {
 render(<MemoryRouter initialEntries={previous?[previous,initial]:[initial]} initialIndex={previous?1:0}>
  <Routes><Route path="/before" element={<p>之前的页面</p>}/><Route path="*" element={<AppRoutes/>}/></Routes>
  <Location/>
 </MemoryRouter>);
}

describe('routes',()=>{
 it.each([
  ['/ai?own=own#scope','/ai/analysis?own=own#scope'],
  ['/ai-settings?provider=text#connections','/ai/settings?provider=text#connections'],
  ['/ai-review?record=r1#evidence','/ai/review?record=r1#evidence'],
  ['/reading-selection?limit=20#records','/ai/reading?limit=20#records'],
  ['/agents?tab=drafts#pending','/ai/agents?tab=drafts#pending'],
  ['/agents/role%20one?tab=drafts&draft=d-1#record','/ai/agents/role%20one?tab=drafts&draft=d-1#record']
 ])('redirects %s while keeping state and replacing the old history entry',async(initial,expected)=>{
  setup(initial,'/before');
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toBe(expected));
  fireEvent.click(screen.getByRole('button',{name:'返回上一页'}));
  await waitFor(()=>expect(screen.getByTestId('location').textContent).toBe('/before'));
 });

 it.each(navItems.filter(item=>!item.external).map(item=>[item.label,item.to]))('opens the page behind sidebar item %s',async(_label,to)=>{
  setup(to);
  await waitFor(()=>expect(screen.queryByRole('heading',{name:'页面不存在'})).toBeNull());
  expect(screen.getByTestId('location').textContent).toBe(to);
 });

 it.each([['/','全链路内容'],['/?view=data','全链路内容'],['/analysis/clusters','账号簇内容'],['/analysis/leaderboard','排行榜内容'],['/processing','标注内容'],
  ['/processing/bots','账号识别内容'],['/processing/risk','风险评估内容'],['/strategy','规则内容（策略）'],['/results/attribution','归因内容'],['/results/exposure','曝光内容'],
  ['/settings','设置索引'],['/system/maintenance','维护内容']])('opens %s on its own page',async(path,text)=>{
  setup(path);
  expect(await screen.findByText(text)).toBeTruthy();
 });

 it('serves the pipeline page instead of treating it as an old role address',async()=>{
  setup('/agents/pipeline');
  expect(await screen.findByText('流水线内容')).toBeTruthy();
  expect(screen.getByTestId('location').textContent).toBe('/agents/pipeline');
 });

 it('lists every group on the not-found page',async()=>{
  setup('/no-such-page');
  expect(await screen.findByRole('heading',{level:1,name:'页面不存在'})).toBeTruthy();
  for(const name of ['总览','采集','处理','分析','策略','Agent 执行','效果','系统'])expect(screen.getByRole('heading',{level:2,name})).toBeTruthy();
  expect(screen.getByRole('link',{name:'事实库'}).getAttribute('href')).toBe('/ai/facts');
 });
});

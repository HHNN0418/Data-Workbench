import {Suspense, lazy} from 'react';
import {Route, Routes} from 'react-router-dom';
import {Spin} from 'antd';
import {LegacyRedirect} from './nav/redirects';
import NotFound from './nav/NotFound';
const PipelineHome=lazy(()=>import('./pages/PipelineHome'));
const Posts=lazy(()=>import('./pages/Records').then(m=>({default:m.Posts})));
const Accounts=lazy(()=>import('./pages/Records').then(m=>({default:m.Accounts})));
const News=lazy(()=>import('./pages/Records').then(m=>({default:m.News})));
const Score=lazy(()=>import('./pages/Score'));
const Analysis=lazy(()=>import('./pages/Analysis'));
const AIContentAnalysis=lazy(()=>import('./pages/AIContentAnalysis'));
const Operations=lazy(()=>import('./pages/Operations'));
const Rules=lazy(()=>import('./pages/Rules'));
const Agents=lazy(()=>import('./pages/Agents'));
const AISettings=lazy(()=>import('./pages/AISettings'));
const AIReview=lazy(()=>import('./pages/AIReview'));
const FactBank=lazy(()=>import('./pages/FactBank'));
const ReadingSelection=lazy(()=>import('./pages/ReadingSelection'));
const BlueCandidates=lazy(()=>import('./pages/BlueCandidates'));
const AccountClusters=lazy(()=>import('./pages/AccountClusters'));
const Leaderboard=lazy(()=>import('./pages/Leaderboard'));
const AccountIdentity=lazy(()=>import('./pages/AccountIdentity'));
const RiskAssessment=lazy(()=>import('./pages/RiskAssessment'));
const Processing=lazy(()=>import('./pages/Processing'));
const Strategy=lazy(()=>import('./pages/Strategy'));
const AgentPipeline=lazy(()=>import('./pages/AgentPipeline'));
const Attribution=lazy(()=>import('./pages/Attribution'));
const Exposure=lazy(()=>import('./pages/Exposure'));
const SettingsIndex=lazy(()=>import('./pages/SettingsIndex'));
const Maintenance=lazy(()=>import('./pages/Maintenance'));

// URLs stay as they were (docs/UX_IA_PLAN.md §5); grouping lives in nav/navConfig.ts.
export default function AppRoutes() {
 return <Suspense fallback={<div className="route-loading"><Spin size="large"/></div>}>
  <Routes>
   <Route path="/" element={<PipelineHome/>}/>
   <Route path="/posts" element={<Posts/>}/>
   <Route path="/accounts" element={<Accounts/>}/>
   <Route path="/news" element={<News/>}/>
   <Route path="/analysis" element={<Analysis/>}/>
   <Route path="/analysis/clusters" element={<AccountClusters/>}/>
   <Route path="/analysis/leaderboard" element={<Leaderboard/>}/>
   <Route path="/ai">
    <Route index element={<LegacyRedirect to="/ai/analysis"/>}/>
    <Route path="analysis" element={<AIContentAnalysis/>}/>
    <Route path="reading" element={<ReadingSelection/>}/>
    <Route path="agents" element={<Agents/>}/>
    <Route path="agents/:agentId" element={<Agents/>}/>
    <Route path="review" element={<AIReview/>}/>
    <Route path="facts" element={<FactBank/>}/>
    <Route path="settings" element={<AISettings/>}/>
   </Route>
   <Route path="/ai-settings" element={<LegacyRedirect to="/ai/settings"/>}/>
   <Route path="/ai-review" element={<LegacyRedirect to="/ai/review"/>}/>
   <Route path="/reading-selection" element={<LegacyRedirect to="/ai/reading"/>}/>
   <Route path="/agents" element={<LegacyRedirect/>}/>
   <Route path="/agents/:agentId" element={<LegacyRedirect/>}/>
   <Route path="/score" element={<Score/>}/>
   <Route path="/rules" element={<Rules/>}/>
   <Route path="/blue" element={<BlueCandidates/>}/>
   <Route path="/processing" element={<Processing/>}/>
   <Route path="/processing/bots" element={<AccountIdentity/>}/>
   <Route path="/processing/risk" element={<RiskAssessment/>}/>
   <Route path="/strategy" element={<Strategy/>}/>
   <Route path="/agents/pipeline" element={<AgentPipeline/>}/>
   <Route path="/results/attribution" element={<Attribution/>}/>
   <Route path="/results/exposure" element={<Exposure/>}/>
   <Route path="/settings" element={<SettingsIndex/>}/>
   <Route path="/system/maintenance" element={<Maintenance/>}/>
   <Route path="/operations" element={<Operations/>}/>
   <Route path="*" element={<NotFound/>}/>
  </Routes>
 </Suspense>;
}

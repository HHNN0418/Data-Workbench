import {Link} from 'react-router-dom';
import {Button, Progress} from 'antd';
import {ArrowRightOutlined} from '@ant-design/icons';
import {useApi, type PageResult} from '../api';
import {Panel, Metrics, QueryState, time, fmt, label} from '../common';
// The former cockpit, now the "数据总览" tab of the home page; the pace warning sits above the tabs there.
export default function Overview(){
 // Summary numbers change slowly; a minute is fresh enough and avoids constant reloads.
 const query=useApi('/overview',{},60000);const recent=useApi<PageResult>('/posts',{limit:5},60000);
 return <QueryState query={query}>{data=>{
 const total=data.counts.posts;const covered=total-data.quality.posts_without_counters;
 return <div className="stack">
 <div className="toolbar overview-head"><span className="muted">最近观测 {time(data.last_observed)}</span><Link to="/analysis"><Button type="primary" icon={<ArrowRightOutlined/>} iconPlacement="end">进入分析</Button></Link></div>
 <Metrics items={[{label:'已采集帖子',value:total,hint:`${fmt(data.counts.accounts)} 个公开账号`},{label:'互动快照',value:data.counts.counter_snapshots,hint:`覆盖 ${covered} 篇帖子`},{label:'官方总分',value:data.score.official_total,hint:'本地登记的官方快照'},{label:'新闻条目',value:data.counts.news_articles,hint:`${data.counts.news_full_bodies} 篇已归档全文`}]}/>
 <div className="overview-grid"><Panel title="最近发布" extra={<Link to="/posts">全部帖子 <ArrowRightOutlined/></Link>}><QueryState query={recent}>{result=>result.items.length?<div className="post-list">{result.items.map(post=><Link className="post-preview" key={post.post_id} to={`/posts?post=${post.post_id}`}><div className="post-avatar">{String(post.username||'?').slice(0,1).toUpperCase()}</div><div><div className="post-byline"><strong>{post.username||`账号 ${post.author_id}`}</strong><span>{time(post.created_at)}</span></div><p>{post.body||'无正文'}</p><span className="muted">{label(post.kind)} · 点赞 {fmt(post.latest_counts?.like_count)} · 回复 {fmt(post.latest_counts?.reply_count)}</span></div><ArrowRightOutlined/></Link>)}</div>:<p className="muted">尚未采集帖子</p>}</QueryState></Panel>
 <div className="stack"><Panel title="数据覆盖"><div className="coverage"><strong>{covered}<small> / {total}</small></strong><span>帖子已有互动观测</span><Progress percent={total?Math.round(covered/total*100):0} showInfo={false} strokeColor="#26705b"/></div><div className="stat-row"><span>归属待确认</span><Link to="/accounts">{data.quality.unknown_accounts} 个账号</Link></div><div className="stat-row"><span>积分流水待解析</span><Link to="/score">{data.score.activity.unparsed} 条</Link></div></Panel>
 <Panel title="最近 5 分钟采集"><p className="muted">最近请求 {time(data.traffic?.last_request_at)}</p><div className="stat-row"><span>真实请求总量</span><b>{fmt(data.traffic?.current?.requests)}</b></div><div className="stat-row"><span>429 限流</span><b>{fmt(data.traffic?.current?.rate_limited)}</b></div><div className="stat-row"><span>请求 / 分钟</span><b>{fmt(data.traffic?.current?.per_minute)}</b></div><Link className="text-action" to="/operations">查看采集详情 <ArrowRightOutlined/></Link></Panel></div></div>
 </div>}}</QueryState>
}

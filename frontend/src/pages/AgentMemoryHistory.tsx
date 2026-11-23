import {useState} from 'react';
import {Link} from 'react-router-dom';
import {Alert,Button,Empty,Input,Select,Skeleton,Space,Tag} from 'antd';
import {useApi} from '../api';
import {time} from '../common';

export type MemorySource={source_id?:string;id?:string;title?:string};
type HistoryItem={id:string;kind:string;at?:string;title?:string;excerpt?:string;epistemic_status?:string;source_ids?:string[];author_id?:number;evidence?:MemorySource[];sources?:MemorySource[];state?:string;action?:string;origin?:string;previous_text?:string;historical?:boolean};
type HistoryPage={items:HistoryItem[];total:number;query?:string;counts?:Record<string,number>;retention?:string;offset?:number;limit?:number;has_more?:boolean};
const kinds=[{value:'all',label:'全部回忆'},{value:'reading',label:'阅读经历'},{value:'outcome',label:'操作结果'},{value:'viewpoints',label:'长期观点'},{value:'questions',label:'持续关注的问题'},{value:'people',label:'对人的印象'}];
const kindLabels=Object.fromEntries(kinds.map(item=>[item.value,item.label]));
const actionLabels:Record<string,string>={post:'发帖',reply:'回复',repost:'转发',like:'点赞',unlike:'取消点赞',follow:'关注',unfollow:'取消关注',skip:'跳过',delete_post:'删除本人帖子'};
const stateLabels:Record<string,string>={published:'已核实完成',completed:'已核实完成',succeeded:'已核实完成',success:'已核实完成',pending:'待执行，尚未完成',draft:'待审阅，尚未完成',review:'待审阅，尚未完成',sending:'正在执行，尚未确认',uncertain:'结果待核对',failed:'执行失败',rejected:'已拒绝',discarded:'已放弃，未执行',cancelled:'已取消',skipped:'已跳过'};

export function MemorySources({ids=[],sources=[]}:{ids?:string[];sources?:MemorySource[]}){
 const unique=[...new Set(ids)].filter(id=>typeof id==='string');
 if(!unique.length)return null;
 return <Space className="agents-memory-sources" size={[12,5]} wrap><span className="muted">依据来源</span>{unique.map(id=>{
  const title=sources.find(source=>(source.source_id||source.id)===id)?.title;
  if(/^post:\d+$/.test(id))return <Link key={id} to={`/posts?post=${id.slice(5)}`}>帖子 #{id.slice(5)} ↗</Link>;
  if(id.startsWith('news:')&&title)return <Link key={id} to={`/news?q=${encodeURIComponent(title)}`}>新闻：{title} ↗</Link>;
  return <span key={id}>{id.startsWith('news:')?`新闻来源 ${id.slice(5)}`:id}</span>;
 })}</Space>;
}

export default function AgentMemoryHistory({agentId,agentName}:{agentId:string;agentName:string}){
 const [search,setSearch]=useState(''),[q,setQ]=useState(''),[kind,setKind]=useState('all'),[offset,setOffset]=useState(0);
 const limit=20,query=useApi<HistoryPage>('/agents/memory-history',{agent_id:agentId,q,kind,limit,offset},15000);
 const data=query.data,items=Array.isArray(data?.items)?data.items:[],total=data?.total??0;
 return <section className="agents-memory-history stack" aria-label={`${agentName}的历史回忆`}>
  <p className="muted">查找 {agentName} 过去读过的内容、操作结果和形成的想法。长期保存从本次升级开始；此前已淘汰的内容可能无法找回。清空记忆会同时清除这里的回忆。</p>
  <div className="agents-memory-history-filters"><Select aria-label="筛选回忆类型" value={kind} options={kinds} onChange={value=>{setKind(value);setOffset(0);}}/><Input.Search aria-label="查找历史回忆" placeholder="搜索话题、内容或人物" maxLength={200} value={search} onChange={event=>setSearch(event.target.value)} onSearch={()=>{setQ(search.trim());setOffset(0);}} allowClear enterButton="查找"/></div>
  {query.isError?<Alert showIcon type="error" title="历史回忆读取失败" description={query.error.message} action={<Button aria-label="重新读取历史回忆" onClick={()=>void query.refetch()}>重试</Button>}/>:query.isPending?<Skeleton active paragraph={{rows:5}}/>:!items.length?<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={q||kind!=='all'?'没有找到符合条件的回忆，可以换个关键词或类型。':'还没有保存历史回忆，角色阅读和思考后会逐步积累。'}/>:<div className="agents-memory-list">{items.map(item=><article key={item.id}>
   <div className="agents-section-head"><strong>{item.title||kindLabels[item.kind]||'一段回忆'}</strong><Space size={5} wrap><Tag>{kindLabels[item.kind]||'回忆'}</Tag>{item.kind==='outcome'&&<><Tag>{actionLabels[item.action||'']||'操作'}</Tag><Tag color={['published','completed','succeeded','success'].includes(item.state||'')?'green':item.state==='uncertain'?'orange':['failed','rejected'].includes(item.state||'')?'red':'default'}>{stateLabels[item.state||'']||'状态待确认'}</Tag>{item.origin==='manual'&&<Tag>人工操作</Tag>}</>}{item.historical&&<Tag>以前的想法</Tag>}{item.at&&<span className="muted">{time(item.at)}</span>}</Space></div>
   {item.excerpt&&<p className="agents-prose">{item.excerpt}</p>}
   {['viewpoints','questions','people'].includes(item.kind)&&<p className="agents-memory-reason">角色的主观理解，会随新资料修正。</p>}
   {item.kind==='reading'&&<p className="agents-memory-reason">这段回忆表示读过原文，内容仍需结合来源判断。</p>}
   {item.kind==='outcome'&&!['published','completed','succeeded','success'].includes(item.state||'')&&<p className="agents-memory-reason">正文和当时的打算不表示操作已经完成，请以核对结果为准。</p>}
   {item.previous_text&&<p className="agents-memory-reason">此前的想法：{item.previous_text}</p>}
   <MemorySources ids={item.source_ids} sources={item.sources||item.evidence}/>
   {!!item.author_id&&<Link to={`/accounts?q=${item.author_id}`}>查看账号 #{item.author_id} ↗</Link>}
  </article>)}</div>}
  <div className="agents-section-head"><span className="muted">{query.isPending?'正在查找回忆…':`匹配 ${total} 条${items.length?`，当前第 ${offset+1}–${offset+items.length} 条`:''}`}</span><Space><Button disabled={!offset||query.isFetching} onClick={()=>setOffset(Math.max(0,offset-limit))}>上一页</Button><Button disabled={query.isFetching||query.isError||!(data?.has_more??offset+items.length<total)} onClick={()=>setOffset(offset+limit)}>下一页</Button></Space></div>
 </section>;
}

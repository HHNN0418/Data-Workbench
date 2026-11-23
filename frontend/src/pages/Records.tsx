import {useState} from 'react';
import {Link, useSearchParams} from 'react-router-dom';
import {Alert, Button, Descriptions, Drawer, Empty, Form, Input, Pagination, Select, Space, Table, Tabs, Tag, Typography} from 'antd';
import type {ColumnsType} from 'antd/es/table';
import {useApi, useAction} from '../api';
import type {PageResult, Row} from '../api';
import {Panel, DataTable, Details, QueryState, Chart, fmt, time, label} from '../common';
import {RiskSummary, RiskTag, useOwnAccounts} from './ownAccounts';

const counterKeys = ['view_count', 'like_count', 'reply_count', 'repost_count'];
const counterLabels:Record<string,string>={pending:'待首次采集',failed:'首次采集失败',partial:'字段缺失',sampled:'已采集'};
const kindLabels:Record<string,string> = {post:'原创',reply:'回复',repost:'转发'};
function useFilters() {
 const [search,setSearch] = useSearchParams();
 const integer = (key:string,fallback:number) => {const n=Number(search.get(key));return Number.isSafeInteger(n)&&n>0?n:fallback;};
 const limit=Math.min(200,integer('limit',25));
 const rawOffset=Number(search.get('offset'));
 const offset=Number.isSafeInteger(rawOffset)&&rawOffset>=0?rawOffset:0;
 const update=(values:Row)=>{const next=new URLSearchParams(search);Object.entries(values).forEach(([key,value])=>{if(value===''||value===null||value===undefined)next.delete(key);else next.set(key,String(value));});setSearch(next);};
 return {search,limit,offset,update};
}
function Pager({data,onChange}:{data:PageResult;onChange:(page:number,size:number)=>void}) {
 return <Pagination current={Math.floor(data.offset/data.limit)+1} pageSize={data.limit} total={data.total} showSizeChanger pageSizeOptions={[25,50,100,200]} showTotal={total=>`共 ${fmt(total)} 条`} onChange={onChange}/>;
}
function AccountName({row}:{row:Row}) {return <div><span>{row.display_name||row.username||`账号 ${row.author_id??row.account_id??'—'}`}</span><div className="muted">{row.team_label||'归属未知'}</div></div>;}

const referenceStatus:Record<string,string> = {pending:'待补采',retry:'稍后重试',unavailable:'当前无法访问'};
function PostEmbed({value}:{value:unknown}) {
 let embed:Row;
 try {embed=typeof value==='string'?JSON.parse(value):value as Row;} catch {return null;}
 if(!embed||typeof embed!=='object')return null;
 let url:string|undefined;
 try {const parsed=new URL(embed.url);if(['https:','http:'].includes(parsed.protocol))url=parsed.href;} catch { /* no safe link */ }
 return <Panel title={embed.site_name||'附带内容'}><Typography.Paragraph>{url?<a href={url} target="_blank" rel="noopener noreferrer">{embed.title||url}</a>:embed.title||'链接不可用'}</Typography.Paragraph>{embed.excerpt&&<Typography.Paragraph>{embed.excerpt}</Typography.Paragraph>}</Panel>;
}
function ReferenceCards({references,onSelect}:{references:Row;onSelect:(id:number)=>void}) {
 return <div className="stack">{Object.entries(references).map(([key,ref])=><Panel key={key} title={`${label(key)} · #${ref.post_id}`}>
  {ref.post?<><Button type="link" onClick={()=>onSelect(ref.post_id)}>查看原帖</Button><Typography.Paragraph style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{ref.post.body||(ref.post.kind==='repost'?'无附言转发':'无文本内容')}</Typography.Paragraph><PostEmbed value={ref.post.embed_json}/></>:<Tag>{referenceStatus[ref.status]||ref.status}</Tag>}
 </Panel>)}</div>;
}

function PostDetail({id,onSelect}:{id:number;onSelect:(id:number)=>void}) {
 const query=useApi(`/posts/${id}`,{},false);
 return <QueryState query={query}>{data=>{
  const post=data.post as Row,counters=(data.counters||[]) as Row[];
  const related=(rows:Row[]) => <Table<Row> size="small" rowKey="post_id" dataSource={rows} pagination={{pageSize:10,hideOnSinglePage:true}} columns={[
   {title:'帖子',dataIndex:'post_id',render:(value:number)=><Button type="link" onClick={()=>onSelect(value)}>#{value}</Button>},
   {title:'正文',dataIndex:'body',ellipsis:true},{title:'发布时间',dataIndex:'created_at',render:time},
  ]}/>;
  return <div className="stack">
   <Descriptions size="small" column={2} items={[
    {key:'author',label:'作者',children:post.username||String(post.author_id??'—')},
    {key:'kind',label:'类型',children:kindLabels[post.kind]||post.kind||'—'},
    {key:'created',label:'发布时间',children:time(post.created_at)},
    {key:'last',label:'最近采集',children:time(post.last_seen)},
   ]}/>
   <Typography.Paragraph style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',fontSize:16}}>{post.body||(post.kind==='repost'?'无附言转发':'无文本内容')}</Typography.Paragraph>
   <PostEmbed value={post.embed_json}/>
   <ReferenceCards references={data.references||{}} onSelect={onSelect}/>
   <Tabs items={[
    {key:'counters',label:`互动 · ${counters.length}`,children:<div className="stack">
     {counters.length?<Chart height={290} option={{tooltip:{trigger:'axis'},legend:{bottom:0},grid:{left:48,right:20,top:20,bottom:65},xAxis:{type:'category',data:counters.map(row=>time(row.observed_at))},yAxis:{type:'value'},series:counterKeys.map(key=>({name:label(key),type:'line',connectNulls:false,data:counters.map(row=>row[key]??null)}))}}/>:<Empty description="尚无互动快照"/>}
     <p className="muted">每次实际采到该帖才形成快照；首条无增长基线。负增量保留，用于检查撤回或计数修正。</p><DataTable rows={counters} columns={['observed_at',...counterKeys]} pageSize={10}/><Panel title="相邻采样的变化"><Table<Row> size="small" rowKey="id" pagination={{pageSize:10}} dataSource={[...counters].reverse()} columns={[{title:'采集时间',dataIndex:'observed_at',render:time},{title:'间隔 / 分钟',dataIndex:'elapsed_minutes',render:fmt},...counterKeys.map(k=>({title:label(k)+'变化',key:k,render:(_:unknown,r:Row)=>r.deltas?.[k]==null?'—':`${r.deltas[k]>0?'+':''}${r.deltas[k]}`}))]}/></Panel>
    </div>},
    {key:'versions',label:`版本 · ${data.versions?.length||0}`,children:<div className="stack">{data.versions?.length?data.versions.map((row:Row)=><Panel key={row.id} title={time(row.observed_at)}><Typography.Paragraph style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{row.body||'无文本内容'}</Typography.Paragraph><Details data={row} title="版本字段"/></Panel>):<Empty description="尚无版本记录"/>}</div>},
    {key:'feed',label:'Feed',children:<div className="stack"><Typography.Text type="secondary">热度分来自 Feed，不代表比赛积分。</Typography.Text><DataTable rows={data.feed_observations||[]} columns={['source','observed_at','rank_position','global_hot_score']}/></div>},
    {key:'relations',label:'关系',children:<div className="stack"><Descriptions column={1} size="small" items={['reply_to_post_id','root_post_id','repost_of_post_id'].map(key=>({key,label:label(key),children:post[key]?<Button type="link" onClick={()=>onSelect(post[key])}>#{post[key]}</Button>:'—'}))}/><Panel title="已采集回复"><Typography.Text type="secondary">{data.reply_coverage?.status==='observed_end'?'上次分页已到末页；不代表实时完整':data.reply_coverage?.status==='partial'?'回复分页补采中':'回复覆盖尚未确认'}</Typography.Text>{related(data.replies||[])}</Panel><Panel title="已采集转发">{related(data.reposts||[])}</Panel></div>},
    {key:'labels',label:'标签',children:<DataTable rows={data.labels||[]}/>},
   ]}/>
   <Details data={post} title="帖子完整字段"/>
  </div>;
 }}</QueryState>;
}

export function Posts() {
 const {search,limit,offset,update}=useFilters();
 const selectedId=Number(search.get('post'));
 const selected=Number.isSafeInteger(selectedId)&&selectedId>0?selectedId:null;
 const setSelected=(id:number|null)=>update({post:id});
 const filters={q:search.get('q')||'',author_id:search.get('author_id')||'',kind:search.get('kind')||'',own:search.get('own')||'all'};
 const feed=search.get('feed')||'latest';
 const snapshot=search.get('snapshot')||'';
 const query=useApi<PageResult>('/posts',{...filters,feed,snapshot:feed==='trending'?snapshot:'',limit,offset},false);
 const columns:ColumnsType<Row>=[
  ...(feed==='trending'?[{title:'排名',dataIndex:'rank_position',width:80,render:(v:unknown)=>fmt(v)},{title:'热度',dataIndex:'global_hot_score',width:110,render:(v:unknown)=>fmt(v)}]:[]),
  {title:'帖子',dataIndex:'body',width:380,render:(body:string,row:Row)=><div><Button type="link" style={{padding:0,height:'auto'}} onClick={()=>setSelected(row.post_id)}>#{row.post_id}</Button><Typography.Paragraph ellipsis={{rows:2}} style={{marginBottom:0}}>{body||(row.kind==='repost'?`转发 #${row.repost_of_post_id}：${row.references?.repost_of_post_id?.post?.body||referenceStatus[row.references?.repost_of_post_id?.status]||'无文本内容'}`:'无文本内容')}</Typography.Paragraph></div>},
  {title:'作者',dataIndex:'author_id',width:160,render:(_:unknown,row:Row)=><AccountName row={row}/>},
  {title:'类型',dataIndex:'kind',width:80,render:(value:string)=>kindLabels[value]||value||'—'},
  ...counterKeys.map(key=>({title:label(key),key,width:90,align:'right' as const,render:(_:unknown,row:Row)=>fmt(row.latest_counts?.[key])})),
  {title:'发布时间',dataIndex:'created_at',width:180,render:time},
  {title:'互动采集',key:'observed',width:180,render:(_:unknown,row:Row)=><div>{counterLabels[row.counter_state]}<div className="muted">{time(row.latest_counts?.observed_at)}</div></div>},
 ];
 return <><Panel title="帖子库" extra={<Button href="/api/export?table=posts">导出全部帖子 CSV</Button>}><div className="stack">
  <Tabs activeKey={feed} onChange={value=>update({feed:value,offset:0,snapshot:null})} items={[{key:'latest',label:'Latest · 最新'},{key:'trending',label:'Trending · 热门'},{key:'all',label:'全部已采集'}]}/>
  {feed==='trending'&&<div className="toolbar"><Select aria-label="Trending 观测页" style={{width:390,maxWidth:'100%'}} value={snapshot} onChange={value=>update({snapshot:value,offset:0})} options={[{value:'',label:'最新榜首观测'},...(query.data?.feed_batches||[]).map((b:Row)=>({value:b.observed_at,label:`${time(b.observed_at)} · 排名 ${b.rank_from??'—'}–${b.rank_to??'—'}（${b.posts} 条）`}))]}/><span className="muted">每次仅展示同一观测页，不混合不同时刻的排名。热度不是积分。</span></div>}
  {feed==='latest'&&<span className="muted">按发布时间从新到旧；增量补采与历史回填分别推进。</span>}
  {query.data&&<div className="toolbar"><Tag>{query.data.coverage?.['feed:latest']?.pending?'历史回填中':'历史分页暂无待续断点'}</Tag>{query.data.coverage?.['feed:latest:live']?.pending&&<Tag color="orange">新帖增量补采中</Tag>}{feed==='trending'&&<span className="muted">观测时间：{time(query.data.feed_observed_at)} · 默认仅为榜首观测页，非完整榜单</span>}</div>}

  <Form key={JSON.stringify(filters)} initialValues={filters} onFinish={values=>update({...values,offset:0})} layout="inline" className="toolbar">
   <Form.Item name="q"><Input aria-label="搜索帖子" placeholder="正文关键词 / 帖子 ID" allowClear/></Form.Item>
   <Form.Item name="author_id" rules={[{validator:(_,value)=>!value||(/^\d+$/.test(value)&&Number.isSafeInteger(Number(value))&&Number(value)>0)?Promise.resolve():Promise.reject(new Error('请输入有效的正整数账号 ID'))}]}><Input aria-label="作者 ID" placeholder="作者 ID" style={{width:130}} allowClear/></Form.Item>
   <Form.Item name="kind"><Select aria-label="内容类型" style={{width:115}} options={[{value:'',label:'全部类型'},...Object.entries(kindLabels).map(([value,label])=>({value,label}))]}/></Form.Item>
   <Form.Item name="own"><Select aria-label="账号归属" style={{width:130}} options={[{value:'all',label:'全部归属'},{value:'own',label:'已登记本队'},{value:'unknown',label:'归属未知'}]}/></Form.Item>
   <Button type="primary" htmlType="submit">查询</Button>
  </Form>
  <QueryState query={query}>{data=><><Table<Row> size="middle" rowKey="post_id" columns={columns} dataSource={data.items} pagination={false} scroll={{x:1240}}/><Pager data={data} onChange={(page,size)=>update({offset:size===limit?(page-1)*size:0,limit:size})}/></>}</QueryState>
 </div></Panel><Drawer title={selected===null?'帖子详情':`帖子 #${selected}`} open={selected!==null} onClose={()=>setSelected(null)} size="large" destroyOnHidden>{selected!==null&&<PostDetail key={selected} id={selected} onSelect={setSelected}/>}</Drawer></>;
}

export function Accounts() {
 const {search,limit,offset,update}=useFilters();
 const [registration,setRegistration]=useState(false);
 const query=useApi<PageResult>('/accounts',{q:search.get('q')||'',limit,offset},false);
 const bootstrap=useApi('/bootstrap',{},false);
 const action=useAction('/team-accounts');
 const [form]=Form.useForm();
 const [selected,setSelected]=useState<Row|null>(null);
 const ownAccounts=useOwnAccounts();
 const columns:ColumnsType<Row>=[
  {title:'账号',dataIndex:'account_id',render:(id:number,row:Row)=><div><Button type="link" style={{padding:0}} onClick={()=>setSelected(row)}>{row.display_name||row.username||`账号 ${id}`}</Button><RiskTag risk={ownAccounts.risk(id)}/><div className="muted">#{id}{row.username?` · @${row.username}`:''}</div></div>},
  {title:'归属',dataIndex:'team_label',render:(value:string)=>value?<Tag>{value}</Tag>:<Typography.Text type="secondary">归属未知</Typography.Text>},
  {title:'已采集帖子',dataIndex:'post_count',align:'right',render:fmt},
  {title:'最近采集',dataIndex:'last_seen',render:time},
 ];
 return <><Panel title="账号" extra={<Space wrap><Button href="/api/export?table=accounts">导出 CSV</Button><Button type="primary" onClick={()=>{action.reset();setRegistration(true);}}>登记本队账号</Button></Space>}><div className="stack">
  {action.isSuccess&&<Alert type="success" title="本队账号归属已保存" showIcon/>}
  <RiskSummary risks={ownAccounts.risks}/>
  <Input.Search key={search.get('q')||''} aria-label="搜索账号" placeholder="用户名 / 显示名 / 账号 ID" defaultValue={search.get('q')||''} allowClear onSearch={q=>update({q,offset:0})} style={{maxWidth:400}}/>
  <QueryState query={query}>{data=><><Table<Row> rowKey="account_id" dataSource={data.items} columns={columns} pagination={false} scroll={{x:680}}/><Pager data={data} onChange={(page,size)=>update({offset:size===limit?(page-1)*size:0,limit:size})}/></>}</QueryState>
 </div></Panel>
 <Drawer title="登记本队账号" open={registration} onClose={()=>setRegistration(false)} destroyOnHidden><Form form={form} layout="vertical" initialValues={{team_label:bootstrap.data?.team_label||''}} onFinish={async values=>{try{await action.mutateAsync({account_ids:[...new Set(values.account_ids.trim().split(/[\s,，]+/).map(Number))],evidence:values.evidence.trim(),team_label:values.team_label.trim()});form.resetFields();setRegistration(false);}catch{/* Error displayed below. */}}}>
  <Typography.Paragraph type="secondary">仅保存本地归属信息，请填写可核验的依据。</Typography.Paragraph>
  <Form.Item label="账号 ID" name="account_ids" rules={[{required:true,message:'请输入账号 ID'},{validator:(_,value)=>{const ids=String(value||'').trim().split(/[\s,，]+/);return ids.length<=1000&&ids.every(id=>/^\d+$/.test(id)&&Number.isSafeInteger(Number(id))&&Number(id)>0)?Promise.resolve():Promise.reject(new Error('输入正整数 ID，多个 ID 用逗号或空格分隔（最多 1,000 个）'));}}]}><Input.TextArea rows={3} placeholder="多个 ID 用逗号或空格分隔"/></Form.Item>
  <Form.Item label="队伍名称" name="team_label" rules={[{required:true,whitespace:true,message:'请输入队伍名称'}]}><Input/></Form.Item>
  <Form.Item label="归属依据" name="evidence" rules={[{required:true,whitespace:true,message:'填写来源或核验依据'}]}><Input.TextArea rows={4} placeholder="来源、核验日期与依据"/></Form.Item>
  {action.error&&<Alert type="error" title={action.error.message} showIcon/>}
  <Button type="primary" htmlType="submit" loading={action.isPending}>保存登记</Button>
 </Form></Drawer>
 <Drawer title={selected?.display_name||selected?.username||'账号详情'} open={!!selected} onClose={()=>setSelected(null)} destroyOnHidden>{selected&&<div className="stack"><Descriptions column={1} items={['account_id','username','team_label','evidence','post_count','first_seen','last_seen'].map(key=>({key,label:label(key),children:key.endsWith('seen')?time(selected[key]):key==='account_id'?String(selected[key]):fmt(selected[key])}))}/><Details data={selected} title="账号完整字段"/></div>}</Drawer></>;
}

function NewsArticles() {
 const {search,limit,offset,update}=useFilters();
 const query=useApi<PageResult>('/news',{q:search.get('q')||'',limit,offset},false);
 const [selected,setSelected]=useState<Row|null>(null);
 return <><div className="stack">
  <Input.Search key={search.get('q')||''} aria-label="搜索新闻" placeholder="搜索标题或已采集正文" defaultValue={search.get('q')||''} allowClear onSearch={q=>update({q,offset:0})} style={{maxWidth:400}}/>
  <Typography.Paragraph type="secondary" style={{marginBottom:0}}>剧情发布时间来自源站，可能领先现实时间；首次采集为本系统发现时间，均按悉尼时区显示。</Typography.Paragraph>
  <QueryState query={query}>{data=><><Table<Row> rowKey="article_key" dataSource={data.items} pagination={false} scroll={{x:1050}} columns={[
   {title:'标题',dataIndex:'title',render:(title:string,row:Row)=><Button type="link" style={{whiteSpace:'normal',textAlign:'left',height:'auto',padding:0}} onClick={()=>setSelected(row)}>{title||'无标题'}</Button>},
   {title:'来源',dataIndex:'site',width:160,render:fmt},
   {title:'正文',dataIndex:'body',width:100,render:(body:string)=>body?<Tag>已采集</Tag>:<Typography.Text type="secondary">待采集</Typography.Text>},
   {title:'剧情发布时间（悉尼）',dataIndex:'published_at',width:205,render:time},
   {title:'首次采集（悉尼）',dataIndex:'first_seen',width:190,render:time},
  ]}/><Pager data={data} onChange={(page,size)=>update({offset:size===limit?(page-1)*size:0,limit:size})}/></>}</QueryState>
 </div><Drawer title={selected?.title||'新闻'} open={!!selected} onClose={()=>setSelected(null)} size="large" destroyOnHidden>{selected&&<div className="stack"><Descriptions size="small" column={1} items={[
  {key:'site',label:'来源',children:fmt(selected.site)},
  ...(selected.author?[{key:'author',label:'作者',children:fmt(selected.author)}]:[]),
  {key:'published',label:'剧情发布时间（悉尼）',children:time(selected.published_at)},
  {key:'first',label:'首次采集（悉尼）',children:time(selected.first_seen)},
  {key:'last',label:'最近采集（悉尼）',children:time(selected.last_seen)},
 ]}/>{selected.body?<Typography.Paragraph style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',fontSize:16}}>{selected.body}</Typography.Paragraph>:<Empty description="仅已采集新闻元数据，正文尚未采集"/>}<Details data={selected} title="新闻完整字段"/></div>}</Drawer></>;
}

const tagName=(value:unknown)=>`#${String(value??'').replace(/^#+/,'')}`;
function TagPostLink({name}:{name:unknown}) {return <Link to={`/posts?feed=all&q=${encodeURIComponent(tagName(name))}`}>{tagName(name)}</Link>;}
function SocialContext({view}:{view:'tags'|'news_accounts'}) {
 const {search,limit,offset,update}=useFilters();
 const popular=search.get('popular')!=='false';
 const q=search.get('q')||'';
 const query=useApi<PageResult>('/social-context',{view,q,limit,offset,...(view==='tags'?{popular}:{})},false);
 const [selected,setSelected]=useState<Row|null>(null);
 return <><div className="stack">
  {view==='tags'&&<Tabs activeKey={popular?'popular':'collected'} onChange={value=>update({popular:value==='popular',offset:0})} items={[{key:'popular',label:'热门标签'},{key:'collected',label:'全部已采标签'}]}/>}
  <Input.Search key={`${view}:${q}`} aria-label={view==='tags'?'搜索标签':'搜索新闻账号帖子'} placeholder={view==='tags'?'标签名称（可带 #）':'正文关键词 / 新闻账号'} defaultValue={q} allowClear onSearch={value=>update({q:value,offset:0})} style={{maxWidth:400}}/>
  <QueryState query={query}>{data=>view==='tags'?<>
   <div className="toolbar"><Tag>最新热门榜：{fmt(data.popular_tags?.count??data.popular_items?.length??0)} 个标签</Tag><span className="muted">榜单观测：{time(data.popular_tags?.observed_at??data.observed_at)}</span><span className="muted">目录最近采集：{time(data.all_tags?.observed_at)}</span>{data.all_tags?.cursor&&<Tag color="orange">目录分页补采中</Tag>}</div>
   <Typography.Paragraph type="secondary" style={{marginBottom:0}}>这里只展示已采集的标签和最新一次热门榜单。{data.all_tags?.completed_at?`上次目录分页完成：${time(data.all_tags.completed_at)}；平台目录仍可能变化。`:'标签目录尚无完整分页记录，不代表平台全部标签。'} 点击标签可搜索本地已采集帖子。</Typography.Paragraph>
   <Table<Row> rowKey="tag_id" dataSource={data.items} pagination={false} scroll={{x:580}} columns={[
    ...(popular?[{title:'排名',key:'rank',width:80,render:(_:unknown,row:Row)=>fmt(row.popular_rank??row.rank)}]:[]),
    {title:'标签',dataIndex:'name',render:(name:string)=><TagPostLink name={name}/>},
    ...(popular?[{title:'平台帖子数（观测）',dataIndex:'post_count',width:180,align:'right' as const,render:fmt},{title:'榜单观测时间',dataIndex:'observed_at',width:190,render:time}]:[{title:'首次采集',dataIndex:'first_seen',width:190,render:time},{title:'最近采集',dataIndex:'last_seen',width:190,render:time}]),
   ]} locale={{emptyText:popular?'尚未采集热门标签榜单':'尚未采集标签目录'}}/>
   <Pager data={data} onChange={(page,size)=>update({offset:size===limit?(page-1)*size:0,limit:size})}/>
  </>:<>
   <div className="stack">{(data.news_accounts||[]).map((account:Row)=><div key={account.username} className="toolbar"><Typography.Text strong>@{account.username}</Typography.Text><Tag>已采集 {fmt(account.posts_collected??account.posts??0)} 条帖子</Tag><span className="muted">最近采集：{time(account.observed_at??account.updated_at)}</span>{account.cursor?<Tag color="orange">历史分页补采中</Tag>:<Tag>{account.completed_at?'上次历史分页已到末页':'历史分页尚未完成'}</Tag>}{account.account_id&&<Link to={`/posts?feed=all&author_id=${account.account_id}`}>查看该账号帖子</Link>}</div>)}</div>
   <Typography.Paragraph type="secondary" style={{marginBottom:0}}>新闻账号的已采集帖子可作为发帖、回复时的引用资料。引用前可查看正文与帖子详情；采集数量不代表账号实时全部帖子。</Typography.Paragraph>
   <Table<Row> rowKey="post_id" dataSource={data.items} pagination={false} scroll={{x:650}} columns={[
    {title:'正文',dataIndex:'body',render:(body:string,row:Row)=><div><Typography.Paragraph ellipsis={{rows:2}} style={{marginBottom:4}}>{body||'无文本内容'}</Typography.Paragraph><Button type="link" style={{padding:0,height:'auto'}} onClick={()=>setSelected(row)}>查看正文 #{row.post_id}</Button></div>},
    {title:'新闻账号',dataIndex:'username',width:190,render:(username:string,row:Row)=><div>{row.display_name||username}<div className="muted">@{username||'—'}</div></div>},
    {title:'发布时间',dataIndex:'created_at',width:190,render:time},
   ]} locale={{emptyText:'尚未采集新闻账号帖子'}}/>
   <Pager data={data} onChange={(page,size)=>update({offset:size===limit?(page-1)*size:0,limit:size})}/>
  </>}</QueryState>
 </div><Drawer title={selected?`新闻账号帖子 #${selected.post_id}`:'新闻账号帖子'} open={!!selected} onClose={()=>setSelected(null)} size="large" destroyOnHidden>{selected&&<div className="stack"><Typography.Text type="secondary">@{selected.username} · {time(selected.created_at)}</Typography.Text><Typography.Paragraph style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',fontSize:16}}>{selected.body||'无文本内容'}</Typography.Paragraph><PostEmbed value={selected.embed_json}/><Link to={`/posts?feed=all&post=${selected.post_id}`}>查看帖子详情 #{selected.post_id}</Link></div>}</Drawer></>;
}

export function News() {
 const {search,update}=useFilters();
 const value=search.get('news_view');
 const view=value==='tags'||value==='news_accounts'?value:'articles';
 return <Panel title="新闻与话题" extra={view==='articles'?<Button href="/api/export?table=news">导出新闻 CSV</Button>:undefined}><div className="stack">
  <Tabs activeKey={view} onChange={news_view=>update({news_view,q:null,offset:0})} items={[{key:'articles',label:'新闻文章'},{key:'news_accounts',label:'新闻账号'},{key:'tags',label:'话题标签'}]}/>
  {view==='articles'?<NewsArticles/>:<SocialContext key={view} view={view}/>}
 </div></Panel>;
}

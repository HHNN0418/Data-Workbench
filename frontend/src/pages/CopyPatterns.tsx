import {Link} from 'react-router-dom';
import {Alert, Table, Tag} from 'antd';
import type {Row} from '../api';
import {Metrics, Panel, fmt, time} from '../common';

// 文案识别 (copy_patterns.py, GET /api/risk/overview → copy_patterns): wording every account's posts share over 48 hours —
// 6-word phrases at least 3 accounts used, openings, clause frames, fact copies — and our roles' template posts against
// post_visibility. Display and risk warning only (D47): nothing here feeds writing or suggests wording.
const groups=['ours','teammates','others','npc'] as const;
const groupText:Record<string,string>={ours:'我方',teammates:'队友',others:'别队',npc:'已校准 NPC'};
const groupColor:Record<string,string|undefined>={ours:'blue',teammates:'cyan',npc:'green'};
const splitText:Record<string,string>={shared:'带不带',coverage:'覆盖率',fact:'照抄事实',opening:'开头'};
const pct=(value:unknown)=>typeof value==='number'?`${(value*100).toFixed(value>0&&value<0.1?1:0)}%`:'—';
const account=(row:Row)=><Link to={`/accounts?q=${row.account_id}`}>{row.username?`@${row.username}`:`#${row.account_id}`}</Link>;
const groupTag=(group:string)=><Tag color={groupColor[group]}>{groupText[group]||group}</Tag>;

// "我方 2 号 5 条 · 别队 1 号 1 条" for the groups that used it.
function Spread({value}:{value?:Row}) {
 const parts=groups.filter(group=>value?.[group]?.posts).map(group=>`${groupText[group]} ${fmt(value?.[group]?.accounts)} 号 ${fmt(value?.[group]?.posts)} 条`);
 return <span>{parts.join(' · ')||'—'}</span>;
}

const lineColumns=[
 {title:'文字',dataIndex:'text'},
 {title:'帖子',dataIndex:'posts',align:'right' as const,render:(value:unknown)=>fmt(value)},
 {title:'账号',dataIndex:'accounts',align:'right' as const,render:(value:unknown)=>fmt(value)},
 {title:'分布',dataIndex:'groups',render:(value:Row)=><Spread value={value}/>},
];
const accountColumns=[
 {title:'账号',dataIndex:'account_id',render:(_:unknown,row:Row)=>account(row)},
 {title:'帖子',dataIndex:'posts',align:'right' as const,render:(value:unknown)=>fmt(value)},
 {title:'带共用短语',dataIndex:'shared_posts',align:'right' as const,render:(value:unknown,row:Row)=>`${fmt(value)}（${pct(row.share)}）`},
 {title:'覆盖率',dataIndex:'coverage',align:'right' as const,render:(value:unknown)=>pct(value)},
 {title:'照抄事实',dataIndex:'fact_posts',align:'right' as const,render:(value:unknown)=>fmt(value)},
 {title:'短语数',dataIndex:'phrases',align:'right' as const,render:(value:unknown)=>fmt(value)},
];

function Members({row}:{row:Row}) {
 const members:Row[]=row.members||[];
 return <div className="stack">
  <span className="muted">用这个短语的账号（{fmt(row.members_total)} 个，每个短语单独一组，不和别的短语合并）：</span>
  <span>{members.map(member=><span key={member.account_id} style={{marginRight:12}}>{account(member)} {groupTag(member.group)}<span className="muted">{fmt(member.posts)} 条</span></span>)}
   {row.members_total>members.length&&<span className="muted">…另有 {fmt(row.members_total-members.length)} 个</span>}</span>
 </div>;
}

export default function CopyPatterns({data}:{data?:Row}) {
 if(!data||data.status!=='ready') {
  return <Panel title="文案识别">
   {data?.last_error?<Alert type="warning" showIcon title={`计算出错（${time(data.last_error.at)}），稍后重试`} description={data.last_error.error}/>
    :<p className="muted">后台每 {fmt(data?.rules?.refresh_minutes??30)} 分钟算一次近 48 小时全部帖子的文案，后端启动几分钟后出第一次结果。</p>}
  </Panel>;
 }
 const rules=data.rules||{},byGroup=data.by_group||{},shared=data.shared||{},facts=data.facts||{},ours=data.ours||{},view=data.accounts_view||{};
 const phrases:Row[]=data.phrases||[],posts:Row[]=ours.posts||[];
 return <div className="stack">
  <Panel title="文案识别 · 共用短语" extra={<span className="muted">近 {fmt(data.window?.hours)} 小时 {fmt(data.posts)} 条帖子 · 计算于 {time(data.generated_at)}{data.run?.cpu_seconds!==undefined&&` · CPU ${fmt(data.run.cpu_seconds)} 秒`}</span>}>
   {data.last_error&&<Alert type="warning" showIcon title={`最近一次计算出错（${time(data.last_error.at)}），显示的是上一次的结果`} description={data.last_error.error}/>}
   <Metrics items={[
    {label:'带共用短语的帖子',value:pct(shared.share),hint:`${fmt(shared.posts)} / ${fmt(data.posts)} 条 · ${fmt(shared.phrases)} 个短语`},
    ...groups.map(group=>({label:groupText[group],value:pct(byGroup[group]?.share),hint:`${fmt(byGroup[group]?.accounts)} 个账号 · 覆盖率 ${pct(byGroup[group]?.coverage)}`}))]}/>
   <Table<Row> size="small" rowKey="phrase" dataSource={phrases} pagination={phrases.length>10?{pageSize:10,showSizeChanger:false}:false} scroll={{x:900}}
    expandable={{expandedRowRender:(row:Row)=><Members row={row}/>}}
    columns={[
     {title:`共用短语（${fmt(rules.gram_words)} 个词，≥${fmt(rules.min_accounts)} 个账号）`,dataIndex:'phrase',render:(text:string,row:Row)=><>{text}{row.npc_free?<Tag color="orange" style={{marginLeft:8}}>NPC 不用</Tag>:<Tag style={{marginLeft:8}}>NPC 也用</Tag>}{row.variants>0&&<span className="muted"> · 另有 {fmt(row.variants)} 个相近片段</span>}</>},
     {title:'账号',dataIndex:'accounts',align:'right' as const,render:(value:unknown)=>fmt(value)},
     {title:'帖子',dataIndex:'posts',align:'right' as const,render:(value:unknown)=>fmt(value)},
     {title:'我方 / 队友 / 别队 / NPC（账号）',dataIndex:'groups',render:(value:Row)=>groups.map(group=>fmt(value?.[group]?.accounts)).join(' / ')},
     {title:'示例',dataIndex:'examples',render:(examples:Row[])=><div className="stack">{(examples||[]).map(item=><span key={item.post_id}><Link to={`/posts?post=${item.post_id}`}>#{item.post_id}</Link> {groupTag(item.group)}<span className="muted">{item.excerpt}</span></span>)}</div>},
    ]}/>
  </Panel>
  <Panel title="开头和固定句式">
   <p className="muted">帖子开头的前 {fmt(rules.opening_words)} 个词，和每个句子、分句开头的前 {fmt(rules.frame_words)} 个词；只列 ≥{fmt(rules.min_accounts)} 个账号用过的。</p>
   <Table<Row> size="small" rowKey="text" dataSource={data.openings||[]} pagination={false} scroll={{x:640}} columns={[{...lineColumns[0],title:'帖子开头'},...lineColumns.slice(1)]}/>
   <Table<Row> size="small" rowKey="text" dataSource={data.frames||[]} pagination={false} scroll={{x:640}} columns={[{...lineColumns[0],title:'句子开头'},...lineColumns.slice(1)]}/>
   <h3>照抄事实原句（连续 ≥{fmt(rules.fact_words)} 个词和事实库相同）</h3>
   <p className="muted">{fmt(facts.posts)} 条帖子照抄了 {fmt(facts.facts_copied)} 条事实：<Spread value={facts.groups}/></p>
   <Table<Row> size="small" rowKey="fact_id" dataSource={facts.top||[]} pagination={false} scroll={{x:760}}
    columns={[{title:'事实',dataIndex:'claim',render:(text:string,row:Row)=><><code>{row.fact_id}</code> {text}</>},
     {title:'帖子',dataIndex:'posts',align:'right' as const,render:(value:unknown)=>fmt(value)},
     {title:'账号',dataIndex:'accounts',align:'right' as const,render:(value:unknown)=>fmt(value)},
     {title:'分布',dataIndex:'groups',render:(value:Row)=><Spread value={value}/>}]}/>
  </Panel>
  <Panel title="账号：带共用短语的帖子占比">
   <ul className="view-notes">
    <li>已校准 NPC：{fmt(view.npc?.accounts)} 个账号里 {fmt(view.npc?.with_shared)} 个用过共用短语，占它们帖子的 {pct(view.npc?.share)}，覆盖率 {pct(view.npc?.coverage)}</li>
    <li>别队：{fmt(view.others?.accounts)} 个账号里 {fmt(view.others?.with_shared)} 个用过共用短语，占它们帖子的 {pct(view.others?.share)}，覆盖率 {pct(view.others?.coverage)}</li>
   </ul>
   {(view.teammates||[]).length>0&&<><h3>队友</h3><Table<Row> size="small" rowKey="account_id" dataSource={view.teammates} pagination={false} scroll={{x:640}} columns={accountColumns}/></>}
   <h3>已校准 NPC（带共用短语最多的）</h3>
   <Table<Row> size="small" rowKey="account_id" dataSource={view.npc?.top||[]} pagination={false} scroll={{x:640}} columns={accountColumns}/>
   <h3>别队（带共用短语最多的）</h3>
   <Table<Row> size="small" rowKey="account_id" dataSource={view.others?.top||[]} pagination={(view.others?.top||[]).length>10?{pageSize:10,showSizeChanger:false}:false} scroll={{x:640}} columns={accountColumns}/>
  </Panel>
  <Panel title="我方：模板帖和隐藏率">
   <Table<Row> size="small" rowKey="key" dataSource={ours.hidden_rates||[]} pagination={false} scroll={{x:640}}
    columns={[{title:'分组',dataIndex:'split',render:(value:string)=>splitText[value]||value},
     {title:'帖子',dataIndex:'label'},
     {title:'条数',dataIndex:'posts',align:'right' as const,render:(value:unknown)=>fmt(value)},
     {title:'已确认隐藏',dataIndex:'hidden',align:'right' as const,render:(value:unknown)=>fmt(value)},
     {title:'待复核',dataIndex:'suspect',align:'right' as const,render:(value:unknown)=>fmt(value)},
     {title:'隐藏率',dataIndex:'hidden_rate',align:'right' as const,render:(value:unknown)=>pct(value)}]}/>
   <Table<Row> size="small" rowKey="account_id" dataSource={ours.roles||[]} pagination={false} scroll={{x:760}}
    columns={[{title:'角色',dataIndex:'name',render:(name:string,row:Row)=><>{name} {account(row)}</>},
     ...accountColumns.slice(1),
     {title:'已确认隐藏',dataIndex:'hidden',align:'right' as const,render:(value:unknown)=>fmt(value)}]}/>
   <h3>我方最常用的共用短语</h3>
   <Table<Row> size="small" rowKey="phrase" dataSource={ours.phrases||[]} pagination={false} scroll={{x:640}}
    columns={[{title:'短语',dataIndex:'phrase',render:(text:string,row:Row)=><>{text}{row.variants>0&&<span className="muted"> · 另有 {fmt(row.variants)} 个相近片段</span>}</>},
     {title:'账号',dataIndex:'accounts',align:'right' as const,render:(value:unknown)=>fmt(value)},
     {title:'分布',dataIndex:'groups',render:(value:Row)=><Spread value={value}/>}]}/>
   <h3>我方带共用短语或照抄事实的帖子 · {fmt(ours.posts_total)}</h3>
   <Table<Row> size="small" rowKey="post_id" dataSource={posts} pagination={posts.length>10?{pageSize:10,showSizeChanger:false}:false} scroll={{x:900}}
    columns={[{title:'帖子',dataIndex:'post_id',render:(id:number)=><Link to={`/posts?post=${id}`}>#{id}</Link>},
     {title:'角色',dataIndex:'name'},
     {title:'状态',dataIndex:'status',render:(status:string)=>status==='hidden'?<Tag color="red">已确认隐藏</Tag>:status==='suspect'?<Tag color="warning">待复核</Tag>:'—'},
     {title:'覆盖率',dataIndex:'coverage',align:'right' as const,render:(value:unknown)=>pct(value)},
     {title:'共用短语',dataIndex:'phrase',render:(text:string,row:Row)=>text?<>{text} <span className="muted">{fmt(row.phrase_accounts)} 个账号</span></>:'—'},
     {title:'照抄事实',dataIndex:'fact_ids',render:(ids:string[])=>(ids||[]).join('、')||'—'},
     {title:'内容',dataIndex:'excerpt'}]}/>
  </Panel>
  <ul className="view-notes">{(data.caveats||[]).map((text:string)=><li key={text}>{text}</li>)}</ul>
 </div>;
}

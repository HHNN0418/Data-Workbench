import {Link} from 'react-router-dom';
import {Alert, Table, Tag} from 'antd';
import {useApi, type Row} from '../api';
import {Metrics, Panel, QueryState, fmt, time} from '../common';
import {RiskSummary, type OwnRisk} from './ownAccounts';
import CopyPatterns from './CopyPatterns';
import './stageViews.css';

// Hidden posts of every account (GET /api/risk/overview, risk_assessment.py). The recent feed is compared with the
// posts we collected; a post it no longer lists that answers 404 is a suspect, and a second 404 at least
// rules.confirm_minutes later (10 since 24 Oct) confirms it. Our roles' suspension risk stays with ownAccounts.tsx.
const missingText:Record<string,string>={hidden:'还没有确认隐藏的帖子',actions:'采集到的动作不到 100 次'};
const groupText:Record<string,string>={ours:'我方',teammates:'队友',others:'别队'};
const unitText:Record<string,string>={fast:'扫描最新 6 小时',full:'扫描 6–48 小时',recheck:'到期复核',shadow:'影子复核',legacy:'复核旧记录'};
// Every bound role, suspended and disabled ones too (risk_assessment.all_roles).
const roleStates:Record<string,{text:string;color?:string}>={suspended:{text:'已封号',color:'red'},disabled:{text:'已停用'},active:{text:'在用',color:'blue'}};

const account=(row:Row)=><Link to={`/accounts?q=${row.account_id}`}>{row.username?`@${row.username}`:`#${row.account_id}`}</Link>;
const hiddenColumns=[
 {title:'账号',dataIndex:'account_id',render:(_:unknown,row:Row)=>account(row)},
 {title:'已确认隐藏',dataIndex:'hidden',align:'right' as const,render:(value:unknown)=>fmt(value)},
 {title:'待复核',dataIndex:'suspect',align:'right' as const,render:(value:unknown)=>fmt(value)},
 {title:'采集到的动作',dataIndex:'collected_actions',align:'right' as const,render:(value:unknown)=>fmt(value)},
 {title:'最近发帖',dataIndex:'last_post_at',render:(value:unknown,row:Row)=><>{time(value)}{row.active&&<Tag color="green">仍在发帖</Tag>}</>},
];

function scanText(result?:Row) {
 if(!result)return '还没有运行';
 return `${time(result.at)} · 翻 ${fmt(result.pages)} 页 · 帖子流 ${fmt(result.listed)} 条 / 库里 ${fmt(result.collected)} 条 · 缺 ${fmt(result.missing)} 条 · 新待复核 ${fmt(result.suspect??0)} 条`;
}

function fullText(cycle?:Row) {
 const segments:Row[]=cycle?.segments||[];
 if(!cycle?.started_at)return '还没有运行';
 const sum=(key:string)=>segments.reduce((total,segment)=>total+Number(segment[key]||0),0);
 const progress=cycle.finished_at?`完成于 ${time(cycle.finished_at)}`:`进行中 ${segments.length}/7 段`;
 return `开始于 ${time(cycle.started_at)} · ${progress} · 缺 ${fmt(sum('missing'))} 条 · 新待复核 ${fmt(sum('suspect'))} 条 · 请求 ${fmt(sum('requests'))} 次`;
}

export default function RiskAssessment() {
 const query=useApi<Row>('/risk/overview',{},60000,true);
 return <div className="stack">
  <Alert type="info" showIcon title="对照平台的最新帖子流，找出所有账号被隐藏的帖子" description="帖子流里没有、库里有的帖子才单独问一次平台。第一次 404 记为待复核，过几分钟再查仍是 404 才确认为隐藏（间隔见下方「扫描状态」）；只用匿名公开读取，不发帖、不举报。"/>
  <QueryState query={query}>{data=>{
   const totals=data.totals||{},rules=data.rules||{},monitor=data.monitor||{},others:Row[]=data.others||[],recent:Row[]=data.recent||[];
   const ownRisks:OwnRisk[]=Array.isArray(data.own_risks)?data.own_risks:[];
   return <div className="stack">
    <div className="toolbar"><span className="muted">计算于 {time(data.generated_at)}{monitor.last_unit&&` · 最近一步：${unitText[monitor.last_unit]||monitor.last_unit}（${time(monitor.last_at)}）`}</span>{data.stale&&<Tag color="processing">更新中</Tag>}</div>
    {monitor.last_error&&<Alert type="warning" showIcon title={`扫描出错（${time(monitor.last_error.at)}），一分钟后重试`} description={monitor.last_error.error}/>}
    <Metrics items={[{label:'待复核',value:totals.suspect,hint:`第一次 404，等 ${fmt(rules.confirm_minutes)} 分钟后复核`},
     {label:'已确认隐藏',value:totals.hidden,hint:`24 小时内 ${fmt(totals.hidden_24h)} 条`},
     {label:'有隐藏帖的账号',value:totals.accounts_with_hidden},
     {label:'别队可举报账号',value:totals.others_report_ready,hint:`已确认隐藏 ≥${fmt(rules.account_report_min_hidden)} 条，动作 ≥${fmt(rules.account_report_min_actions)} 次`}]}/>
    <Panel title="扫描状态">
     <ul className="view-notes">
      <li>最新 {fmt(rules.segment_hours)} 小时，每 {fmt(rules.fast_interval_minutes)} 分钟一次：{scanText(monitor.fast)}</li>
      <li>{fmt(rules.segment_hours)}–{fmt(rules.full_hours)} 小时，每 {fmt(rules.full_interval_minutes)} 分钟一轮：{fullText(monitor.full)}</li>
      <li>复核确认（第一次 404 后 {fmt(rules.confirm_minutes)} 分钟再查）：{monitor.recheck?`${time(monitor.recheck.at)} · 到期 ${fmt(monitor.recheck.due)} 条 · 确认 ${fmt(monitor.recheck.hidden??0)} 条 · 恢复可见 ${fmt(monitor.recheck.visible??0)} 条`:'还没有到期的待复核帖子'}</li>
     </ul>
    </Panel>
    <Panel title="我方账号" extra={<Link to="/processing/bots?label=ours">账号识别 · 我方</Link>}>
     {ownRisks.length?<RiskSummary risks={ownRisks}/>:<p className="muted">没有角色接近封号线。</p>}
     {(data.ours||[]).length>0&&<Table<Row> size="small" rowKey="account_id" pagination={false} dataSource={data.ours} scroll={{x:640}}
      columns={[{title:'角色',dataIndex:'name',render:(name:string,row:Row)=><>{name} {account(row)}</>},
       {title:'状态',dataIndex:'role_state',render:(state:string)=>{const label=roleStates[state];return label?<Tag color={label.color}>{label.text}</Tag>:'—';}},
       ...hiddenColumns.slice(1)]}/>}
    </Panel>
    {(data.teammates||[]).length>0&&<Panel title="队友账号" extra={<span className="muted">队友在管，我们只监控</span>}>
     <Table<Row> size="small" rowKey="account_id" pagination={false} dataSource={data.teammates} scroll={{x:640}} columns={hiddenColumns}/>
    </Panel>}
    <Panel title={`别队账号 · ${fmt(data.others_total)}`} extra={<Link to="/blue">Blue 候选</Link>}>
     <Table<Row> size="small" rowKey="account_id" dataSource={others} pagination={others.length>20?{pageSize:20,showSizeChanger:false}:false} scroll={{x:860}}
      columns={[...hiddenColumns,
       {title:'机器人标记',dataIndex:'is_bot',render:(value:boolean)=>value?<Tag color="red">机器人</Tag>:'—'},
       {title:'账号举报条件',dataIndex:'account_report',render:(value:Row)=>value?.ready?<Tag color="green">满足</Tag>:<span className="muted">{(value?.missing||[]).map((key:string)=>missingText[key]||key).join('；')}</span>}]}/>
    </Panel>
    <Panel title="最近的隐藏和待复核帖子">
     <Table<Row> size="small" rowKey="post_id" dataSource={recent} pagination={recent.length>20?{pageSize:20,showSizeChanger:false}:false} scroll={{x:860}}
      columns={[
       {title:'帖子',dataIndex:'post_id',render:(id:number)=><Link to={`/posts?post=${id}`}>#{id}</Link>},
       {title:'账号',dataIndex:'account_id',render:(_:unknown,row:Row)=><>{account(row)} <span className="muted">{groupText[row.group]||row.group}</span></>},
       {title:'状态',dataIndex:'status',render:(status:string)=>status==='hidden'?<Tag color="red">已确认隐藏</Tag>:<Tag color="warning">待复核</Tag>},
       {title:'第一次 404',dataIndex:'first_404_at',render:(value:unknown)=>time(value)},
       {title:'确认时间',dataIndex:'confirmed_at',render:(value:unknown)=>time(value)},
       {title:'内容',dataIndex:'excerpt',render:(text:string)=>text||<span className="muted">无文本</span>},
      ]}/>
    </Panel>
    <CopyPatterns data={data.copy_patterns}/>
    <ul className="view-notes">{(data.caveats||[]).map((text:string)=><li key={text}>{text}</li>)}</ul>
   </div>;
  }}</QueryState>
 </div>;
}

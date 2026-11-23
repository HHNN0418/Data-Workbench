import {Alert,Tag} from 'antd';
import {time} from './common';

export interface CollectionStatus {
 state?:string;
 label?:string;
 healthy?:boolean;
 reason?:string;
 last_collected_at?:string|null;
 last_ingested_at?:string|null;
 heartbeat_at?:string|null;
 last_attempt_at?:string|null;
 http_status?:number|null;
 activity_rows?:number|null;
 age_seconds?:number|null;
 freshness_seconds?:number;
 error?:string|null;
}
interface Props {status?:CollectionStatus;error?:string;heartbeatAt?:string|null}

function presentation({status,error}:Props){
 const verified=typeof status?.healthy==='boolean'&&!!status.state;
 const healthy=verified&&status.healthy===true&&!error;
 const label=error?'状态读取失败':verified?status.label||'状态待核验':'状态待核验';
 const reason=error?`无法确认最新采集状态：${error}`:verified?status.reason||'请查看最近成功采集和入库时间。':'服务端尚未提供成功采集状态，暂时无法确认数据是否及时更新。';
 return {healthy,label,reason,type:healthy?'success' as const:status?.state==='error'?'error' as const:'warning' as const};
}

export function CollectionStatusBadge(props:Props){
 const {healthy,label}=presentation(props);
 return <Tag color={healthy?'green':'warning'}>{label}</Tag>;
}

export function CollectionStatusDetails(props:Props){
 const {status,heartbeatAt}=props;
 const {healthy,label,reason,type}=presentation(props);
 return <div className="stack">
  <Alert type={type} showIcon title={`采集状态：${label}`} description={reason}/>
  <div className="toolbar">
   <span>最近成功采集：{time(status?.last_collected_at)}</span>
   <span>最近入库：{time(status?.last_ingested_at)}</span>
   <span className="muted">连接心跳：{time(status?.heartbeat_at||heartbeatAt)}</span>
  </div>
  {(status?.last_attempt_at||status?.http_status!=null||status?.activity_rows!=null||status?.error)&&<div className="toolbar">
   {status.last_attempt_at&&<span className="muted">最近尝试：{time(status.last_attempt_at)}</span>}
   {status.http_status!=null&&<Tag>HTTP {status.http_status}</Tag>}
   {status.activity_rows!=null&&<span>最近读取流水：{status.activity_rows} 条</span>}
   {status.error&&status.error!==reason&&<span>最近错误：{status.error}</span>}
  </div>}
  <div className="muted">{healthy?'近期成功采集并已入库。':'连接心跳只表示扩展在线，不能证明采集和入库成功。'}{status?.freshness_seconds?`超过 ${Math.round(status.freshness_seconds/60)} 分钟未成功采集会提示停滞。`:''}重复流水或总分未变化也会更新成功采集时间。</div>
 </div>;
}

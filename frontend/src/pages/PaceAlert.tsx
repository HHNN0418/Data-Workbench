import {Link} from 'react-router-dom';
import {Alert} from 'antd';
import type {Row} from '../api';
import {time} from '../common';

const speed=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1});
function perHour(value:unknown) {const n=Number(value);return value===null||value===undefined||!Number.isFinite(n)?'—':`${speed.format(n)} 分/时`;}

// Pace warnings from overview.threshold (C's P0-1): the 6-hour speed is compared with what the next poll needs now.
export default function PaceAlert({threshold}:{threshold?:Row|null}) {
 const poll=threshold?.polls?.[0],rate6=threshold?.rates?.['6h']?.points_per_hour,rate12=threshold?.rates?.['12h']?.points_per_hour,need=poll?.required_points_per_hour_now;
 const behind=rate6!==null&&rate6!==undefined&&need!==null&&need!==undefined&&Number(rate6)<Number(need);
 return <>
  {threshold?.stale&&<Alert type="warning" showIcon title="总分可能过期" description={threshold.observed_at?`最近一次官方总分快照在 ${time(threshold.observed_at)}（${speed.format(Number(threshold.snapshot_age_minutes))} 分钟前），下面的速度按这份快照计算。`:'还没有官方总分快照，速度和预计时间暂时算不出来。'} action={<Link to="/operations">检查积分同步</Link>}/>}
  {behind&&<Alert type="error" showIcon title={`最近 6 小时的得分速度低于 Poll ${poll.poll} 需要的速度`} description={<>
   <div>需要 {perHour(need)} · 最近 6 小时 {perHour(rate6)} · 最近 12 小时 {perHour(rate12)}</div>
   <div>Poll {poll.poll} 约 {time(poll.estimated_count_at)} 计票（还剩 {speed.format(Number(poll.hours_left))} 小时），距 {poll.threshold} 分还差 {poll.gap ?? '—'} 分。</div>
  </>} action={<Link to="/ai/agents">查看角色</Link>}/>}
 </>;
}

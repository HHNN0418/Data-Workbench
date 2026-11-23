import {Link, useLocation} from 'react-router-dom';
import {Alert} from 'antd';
import {Panel} from '../common';

// Pages of the data-flow sidebar whose data is still being built (docs/PIPELINE_UI_SPEC.md §5 S-A). All seven
// planned pages now have their own page (routes.tsx); a future sidebar page can start here with a plan entry.
export interface Plan {stage:string;summary:string;metrics:string[];links:Array<{label:string;to:string}>;source:string}
export const stagePlans:Record<string,Plan>={};

export default function StagePlaceholder() {
 const plan=stagePlans[useLocation().pathname.replace(/\/+$/,'')];
 if(!plan)return null;
 return <div className="stack">
  <Alert type="info" showIcon title="这一段的数据在建设中" description={`${plan.stage} · 数据来源：${plan.source}。上线前可以先从下面的入口查看现有数据。`}/>
  <Panel title="这一页会显示"><p>{plan.summary}</p><ul>{plan.metrics.map(metric=><li key={metric}>{metric}</li>)}</ul></Panel>
  <Panel title="现在可以去"><div className="toolbar">{plan.links.map(link=><Link key={link.to} to={link.to}>{link.label}</Link>)}</div></Panel>
 </div>;
}

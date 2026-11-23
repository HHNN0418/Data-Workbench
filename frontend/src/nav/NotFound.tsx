import {Link} from 'react-router-dom';
import {navGroups, navItems} from './navConfig';

export default function NotFound() {
 return <div className="not-found">
  <h1>页面不存在</h1>
  <p className="muted">地址可能已经改名。可以从下面的入口进入，旧地址会自动跳到新位置。</p>
  <div className="not-found-groups">{navGroups.map(group=><section key={group.key}><h2>{group.label||'总览'}</h2><ul>
   {navItems.filter(item=>item.group===group.key&&!item.external).map(item=><li key={item.key}><Link to={item.to}>{item.label}</Link></li>)}
  </ul></section>)}</div>
 </div>;
}

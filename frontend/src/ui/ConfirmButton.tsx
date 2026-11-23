import type {ReactNode} from 'react';
import {Button, Popconfirm, type ButtonProps} from 'antd';

// Destructive actions ask first (docs/UX_IA_PLAN.md §13.3).
export default function ConfirmButton({title,description,okText='确认',onConfirm,children,...button}:Omit<ButtonProps,'onClick'|'title'>&{title:ReactNode;description?:ReactNode;okText?:string;onConfirm:()=>unknown}) {
 return <Popconfirm title={title} description={description} okText={okText} cancelText="取消" okButtonProps={{danger:true}} onConfirm={async()=>{await onConfirm();}} disabled={button.disabled}>
  <Button danger {...button}>{children}</Button>
 </Popconfirm>;
}

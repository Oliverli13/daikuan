import streamlit as st
import pandas as pd
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from pandas.tseries.offsets import BDay
import io

# ==========================================
# 核心计算引擎 (支持 LPR 动态切片与节假日顺延)
# ==========================================
def run_core_engine(borrower, lender, principal, rate_mode, rate_config, start_date, end_date, day_count_base, generated_plan_df, int_freq_months, int_day, remarks, shift_weekend=True):
    
    def get_exec_date(d):
        if shift_weekend:
            while d.weekday() >= 5:  
                d += timedelta(days=1)
        return d

    actual_end_date = get_exec_date(end_date)
    events = {}
    
    def add_event(nominal_d, p_pay=0.0, is_int=False):
        actual_d = get_exec_date(nominal_d)
        if actual_d not in events: 
            events[actual_d] = {'nominal_dates': set(), 'principal_pay': 0.0, 'is_interest_day': False}
        events[actual_d]['nominal_dates'].add(nominal_d)
        events[actual_d]['principal_pay'] += p_pay
        events[actual_d]['is_interest_day'] = events[actual_d]['is_interest_day'] or is_int

    temp_date = date(start_date.year, start_date.month, 1)
    while temp_date <= actual_end_date + relativedelta(months=12):
        if temp_date > start_date and temp_date <= actual_end_date:
            if int_freq_months == 6 and temp_date.month in [6, 12]:
                try: add_event(date(temp_date.year, temp_date.month, int_day), is_int=True)
                except ValueError: pass
            elif int_freq_months == 3 and temp_date.month in [3, 6, 9, 12]:
                try: add_event(date(temp_date.year, temp_date.month, int_day), is_int=True)
                except ValueError: pass
            elif int_freq_months == 1:
                try: add_event(date(temp_date.year, temp_date.month, int_day), is_int=True)
                except ValueError: pass
        temp_date += relativedelta(months=1)

    for _, row in generated_plan_df.iterrows():
        if pd.notnull(row['还本日期']) and row['还本金额'] > 0:
            add_event(pd.to_datetime(row['还本日期']).date(), p_pay=float(row['还本金额']))
                
    add_event(end_date, is_int=True) 

    repricing_map = {} 
    if rate_mode == "浮动":
        rule = rate_config['rule']
        cycle = rate_config['cycle_months']
        curr_r_date = start_date
        
        while curr_r_date <= actual_end_date:
            if rule == "按放款日对月对日 (合同约定)":
                curr_r_date += relativedelta(months=cycle)
                ref_date = (pd.to_datetime(curr_r_date) - BDay(1)).date()
                repricing_map[curr_r_date] = ref_date
                
            elif rule == "每月20日 (LPR发布日同步)":
                if curr_r_date.day < 20:
                    curr_r_date = date(curr_r_date.year, curr_r_date.month, 20)
                else:
                    tmp = curr_r_date + relativedelta(months=cycle)
                    curr_r_date = date(tmp.year, tmp.month, 20)
                repricing_map[curr_r_date] = curr_r_date
                
            elif rule == "每年1月1日":
                curr_r_date = date(curr_r_date.year + 1, 1, 1)
                ref_date = (pd.to_datetime(curr_r_date) - BDay(1)).date()
                repricing_map[curr_r_date] = ref_date

    def get_lpr(reference_date, curve_df):
        curve_df['生效日期'] = pd.to_datetime(curve_df['生效日期']).dt.date
        # 强制转换为浮点数，防止用户手动输入了非数字字符
        curve_df['LPR(%)'] = pd.to_numeric(curve_df['LPR(%)'], errors='coerce').ffill()
        valid_rates = curve_df[curve_df['生效日期'] <= reference_date]
        if not valid_rates.empty:
            return valid_rates.sort_values(by='生效日期', ascending=False).iloc[0]['LPR(%)'] / 100
        return curve_df.iloc[0]['LPR(%)'] / 100 

    sorted_dates = sorted(list(events.keys()))
    schedule = []
    interest_details = [] 
    
    balance = float(principal)
    prev_date = start_date
    accrued_interest = 0.0
    
    if rate_mode == "固定":
        current_rate = rate_config['fixed_rate'] / 100
    else:
        init_ref_date = (pd.to_datetime(start_date) - BDay(1)).date()
        current_rate = get_lpr(init_ref_date, rate_config['lpr_curve']) + (rate_config['spread_bp'] / 10000)

    slice_start = start_date
    slice_balance = balance

    for current_date in sorted_dates:
        if current_date <= start_date: continue
        
        temp_day = prev_date
        while temp_day < current_date:
            temp_day += timedelta(days=1)
            
            if rate_mode == "浮动" and temp_day in repricing_map:
                slice_days = (temp_day - slice_start).days
                slice_int = slice_balance * current_rate / day_count_base * slice_days
                if slice_days > 0:
                    interest_details.append({
                        '实际结算日': current_date.strftime('%Y/%m/%d'),
                        '计息段': f"{slice_start.strftime('%Y/%m/%d')} 至 {temp_day.strftime('%Y/%m/%d')}",
                        '计息天数': slice_days,
                        '本金余额': slice_balance,
                        '执行年化': f"{current_rate*100:.4f}%",
                        '计算公式': f"{slice_balance:,.2f} × {current_rate*100:.4f}% ÷ {day_count_base} × {slice_days}",
                        '切片利息': slice_int
                    })
                
                ref_date = repricing_map[temp_day]
                current_rate = get_lpr(ref_date, rate_config['lpr_curve']) + (rate_config['spread_bp'] / 10000)
                slice_start = temp_day
                slice_balance = balance
            
            daily_int = balance * current_rate / day_count_base
            accrued_interest += daily_int

        days = (current_date - prev_date).days
        event = events[current_date]
        p_payment = min(event['principal_pay'], balance)
        
        i_payment = 0.0
        if event['is_interest_day'] or p_payment > 0:
            i_payment = accrued_interest
            accrued_interest = 0.0
            
            slice_days = (current_date - slice_start).days
            slice_int = slice_balance * current_rate / day_count_base * slice_days
            if slice_days > 0:
                interest_details.append({
                    '实际结算日': current_date.strftime('%Y/%m/%d'),
                    '计息段': f"{slice_start.strftime('%Y/%m/%d')} 至 {current_date.strftime('%Y/%m/%d')}",
                    '计息天数': slice_days,
                    '本金余额': slice_balance,
                    '执行年化': f"{current_rate*100:.4f}%",
                    '计算公式': f"{slice_balance:,.2f} × {current_rate*100:.4f}% ÷ {day_count_base} × {slice_days}",
                    '切片利息': slice_int
                })
            slice_start = current_date
            
        end_balance = balance - p_payment
        slice_balance = end_balance
        
        if p_payment > 0 or i_payment > 0:
            nominal_str = "\n".join(sorted([d.strftime('%Y/%m/%d') for d in event['nominal_dates']]))
            is_shifted = "⚠️顺延" if nominal_str != current_date.strftime('%Y/%m/%d') else ""
            
            schedule.append({
                '借款方': borrower,
                '理论归属日': nominal_str,
                '实际收付日': current_date.strftime('%Y/%m/%d') + is_shifted,
                '计息天数': days,
                '期末执行利率': f"{current_rate*100:.4f}%",
                '期初本金': balance,
                '应付利息': i_payment if i_payment > 0 else 0,
                '应付本金': p_payment if p_payment > 0 else 0,
                '本息合计': i_payment + p_payment,
                '期末剩余本金': end_balance,
                '台账备注': remarks 
            })
            
        balance, prev_date = end_balance, current_date
        if balance <= 0.01: break

    return pd.DataFrame(schedule), pd.DataFrame(interest_details)

def generate_dates(start_d, end_d, freq_months, target_day):
    dates, curr = [], start_d
    while curr <= end_d:
        dates.append(curr)
        curr += relativedelta(months=freq_months)
        try: curr = date(curr.year, curr.month, target_day)
        except ValueError: pass
    return dates

# 专属 LPR 表格构建器 (根据起止日期动态生成骨架)
def build_lpr_table(start_d, end_d, real_df=None):
    today = date.today()
    skeleton_dates = []
    
    # 骨架起点：放款日前一个月的20号 (确保涵盖首期定价)
    curr = date(start_d.year, start_d.month, 20)
    if start_d.day <= 20:
        curr -= relativedelta(months=1)
    
    # 骨架终点：到期日之后的第一个20号
    while curr <= end_d + relativedelta(months=1):
        skeleton_dates.append(curr)
        curr += relativedelta(months=1)
        
    data_rows = []
    last_valid_lpr = 3.95 # 极端情况的兜底值
    
    if real_df is not None and not real_df.empty:
        real_df = real_df.sort_values('生效日期')
        
    for d in skeleton_dates:
        if real_df is not None and not real_df.empty:
            # 找到在日历日 d 之前最近发布的真实 LPR
            past_lpr = real_df[real_df['生效日期'] <= d]
            if not past_lpr.empty:
                last_valid_lpr = past_lpr.iloc[-1]['实际LPR']
        
        # 区分过去真实数据与未来待预测数据
        status = "✅ 历史已发布" if d <= today else "⚠️ 待更新(请预测)"
        data_rows.append({
            '生效日期': d,
            'LPR(%)': float(last_valid_lpr),
            '发布状态': status
        })
    return pd.DataFrame(data_rows)

# ==========================================
# UI 界面与配置
# ==========================================
st.set_page_config(layout="wide", page_title="专业信贷台账系统")

with st.sidebar:
    st.header("🎛️ 基础业务配置")
    loan_type = st.selectbox("贷款品种", ["固定资产项目贷款", "流动资金贷款", "并购贷款"])
    app_mode = st.selectbox("计息模式", ["📈 LPR 浮动利率", "📊 固定利率"])
    
    st.divider()
    borrower_input = st.text_input("借款方", value="重庆医药集团九隆现代中药有限公司")
    lender_input = st.text_input("出借方", value="招商银行重庆分行")
    principal_input = st.number_input("借款总金额 (元)", value=35000000.0, step=1000000.0)
    
    col_d1, col_d2 = st.columns(2)
    start_input = col_d1.date_input("起息日(放款日)", value=date(2026, 2, 2))
    end_input = col_d2.date_input("理论到期日", value=date(2031, 2, 1))
    
    day_count_base = st.selectbox("计息基数 (算头不算尾)", [360, 365], index=0)
    shift_weekend = st.toggle("🗓️ 启用节假日顺延 (遇周末顺延至下周一)", value=True)
    
    st.subheader("🗓️ 结息规则")
    col_f1, col_f2 = st.columns(2)
    int_freq_label = col_f1.selectbox("结息频率", ["按半年", "按季", "按月"], index=0)
    int_day = col_f2.number_input("结息日(1-31)", min_value=1, max_value=31, value=20)
    int_freq_months = {"按半年": 6, "按季": 3, "按月": 1}[int_freq_label]
    remarks_input = st.text_area("业务备注", value=f"{loan_type}，含1年宽限期。")

st.title(app_mode + "智能台账与计息引擎")
rate_config = {}

if app_mode == "📊 固定利率":
    fixed_rate = st.number_input("固定年化利率 (%)", value=3.00, step=0.01)
    rate_config = {'fixed_rate': fixed_rate}
    mode_flag = "固定"
else:
    mode_flag = "浮动"
    st.info("💡 引擎已按您的【起止日期】自动裁剪 LPR 周期表。对于未来尚未发布的 LPR，状态标记为“待更新”，您可以直接在表单中双击输入预测值进行压力测试。")
    
    col_lpr1, col_lpr2, col_lpr3, col_lpr4 = st.columns(4)
    lpr_tenor = col_lpr1.selectbox("LPR 品种", ["5年期以上", "1年期"], index=0)
    spread_bp = col_lpr2.number_input("加减点 (BP)", value=-46, step=1)
    repricing_rule = col_lpr3.selectbox("重定价日规则", ["按放款日对月对日 (合同约定)", "每月20日 (LPR发布日同步)", "每年1月1日"], index=0)
    repricing_cycle = col_lpr4.selectbox("重定价周期", ["1个月", "3个月", "6个月", "12个月"], index=0)
    cycle_map = {"1个月": 1, "3个月": 3, "6个月": 6, "12个月": 12}
    
    st.subheader("📉 LPR 数据源管理 (精准贴合贷款期限)")
    
    # 首次加载时，初始化一个基于起止日期的虚拟表
    if 'lpr_data' not in st.session_state:
        st.session_state.lpr_data = build_lpr_table(start_input, end_input, None)

    col_btn1, col_btn2 = st.columns([1, 4])
    if col_btn1.button("🌐 同步官网 LPR 并更新周期表"):
        with st.spinner("正在连接外汇交易中心提取数据并拼接预测表..."):
            try:
                import akshare as ak
                real_lpr = ak.macro_china_lpr()
                real_lpr['生效日期'] = pd.to_datetime(real_lpr['TRADE_DATE']).dt.date
                target_col = 'LPR5Y' if lpr_tenor == "5年期以上" else 'LPR1Y'
                real_df = real_lpr[['生效日期', target_col]].rename(columns={target_col: '实际LPR'})
                real_df = real_df.dropna()
                
                # 重新构建带有真实数据的骨架表
                st.session_state.lpr_data = build_lpr_table(start_input, end_input, real_df)
                st.success(f"✅ 拉取成功！已自动填充历史数据，未来的 LPR 请手动预测。")
            except ImportError:
                st.error("❌ 缺少依赖库，请在终端运行: pip install akshare")
            except Exception as e:
                st.error(f"❌ 网络或接口异常: {str(e)}")

    # 渲染带有“待更新”状态的 Data Editor
    lpr_curve_df = st.data_editor(
        st.session_state.lpr_data, 
        num_rows="dynamic", 
        use_container_width=True, 
        hide_index=True,
        column_config={
            "发布状态": st.column_config.TextColumn(disabled=True) # 禁止编辑状态列，仅可编辑 LPR
        }
    )
    rate_config = {'spread_bp': spread_bp, 'rule': repricing_rule, 'cycle_months': cycle_map[repricing_cycle], 'lpr_curve': lpr_curve_df}

st.divider()

# --- 还本计划组装器 (倒轧补齐版) ---
st.subheader("🛠️ 多阶段还本计划组装器 (支持智能倒轧)")
st.caption("系统逻辑：优先锁定阶段3和阶段4的排本金额，阶段2将自动为您补齐剩余本金，确保账面本金刚好清零。")

col_l, col_r = st.columns(2)
plan_records = []

with col_l:
    with st.expander("⏳ 阶段 1：宽限期 (仅结息，不排本)", expanded=True):
        grace_end = st.date_input("宽限期截止日", value=date(2027, 2, 1))

    with st.expander("🛑 阶段 4：尾款结清 / 特殊插队", expanded=True):
        p4_enable = st.checkbox("启用阶段 4", value=True)
        if p4_enable: 
            p4_date = st.date_input("尾款日期", value=date(2031, 2, 1))
            p4_amount = st.number_input("结清金额", value=11000000.0)
            p4_dates, p4_total = ([p4_date], p4_amount) if p4_date > grace_end else ([], 0.0)
        else:
            p4_dates, p4_total = [], 0.0

with col_r:
    with st.expander("📈 阶段 3：阶梯跳跃 / 变额分期", expanded=False):
        p3_enable = st.checkbox("启用阶段 3", value=False)
        if p3_enable:
            c31, c32 = st.columns(2)
            p3_start, p3_end = c31.date_input("起日", value=date(2029, 6, 20), key="p3_s"), c32.date_input("止日", value=date(2030, 12, 20), key="p3_e")
            p3_freq_val = {"按半年": 6, "按季": 3}[st.selectbox("频率", ["按半年", "按季"], key="p3_f")]
            p3_amount = st.number_input("单期金额", value=4000000.0, key="p3_a")
            p3_dates = [d for d in generate_dates(p3_start, p3_end, p3_freq_val, int_day) if d > grace_end]
            p3_total = len(p3_dates) * p3_amount
        else:
            p3_dates, p3_total = [], 0.0

    with st.expander("🌊 阶段 2：常规规律分期 (智能补齐)", expanded=True):
        p2_enable = st.checkbox("启用阶段 2", value=True)
        if p2_enable:
            c21, c22 = st.columns(2)
            p2_start, p2_end = c21.date_input("起日", value=date(2027, 6, 20), key="p2_s"), c22.date_input("止日", value=date(2030, 12, 20), key="p2_e")
            p2_freq_val = {"按半年": 6, "按季": 3, "按月": 1}[st.selectbox("频率", ["按半年", "按季", "按月"], key="p2_f")]
            p2_dates = [d for d in generate_dates(p2_start, p2_end, p2_freq_val, int_day) if d > grace_end]
            p2_count = len(p2_dates)
            
            if st.checkbox("✨ 自动反推单期金额 (倒轧)", value=True):
                calc_amount = max((principal_input - p3_total - p4_total) / p2_count, 0.0) if p2_count > 0 else 0.0
                st.info(f"💡 自动计算：剩余本金 {(principal_input - p3_total - p4_total):,.2f} 元 ÷ {p2_count} 期 = **{calc_amount:,.2f}** 元/期")
                p2_amount = calc_amount
            else:
                p2_amount = st.number_input("手动输入单期金额", value=3000000.0, key="p2_a")

for d in p3_dates: plan_records.append({'还本日期': d, '还本金额': p3_amount})
for d in p4_dates: plan_records.append({'还本日期': d, '还本金额': p4_amount})
if p2_enable:
    for d in p2_dates: plan_records.append({'还本日期': d, '还本金额': p2_amount})

generated_plan_df = pd.DataFrame(plan_records)
if not generated_plan_df.empty:
    generated_plan_df = generated_plan_df.groupby('还本日期', as_index=False).sum()

if st.button(f"🚀 生成高精度业务台账", type="primary", use_container_width=True):
    result_df, details_df = run_core_engine(borrower_input, lender_input, principal_input, mode_flag, rate_config, start_input, end_input, day_count_base, generated_plan_df, int_freq_months, int_day, remarks_input, shift_weekend)
    
    col_m1, col_m2, col_m3 = st.columns(3)
    col_m1.metric("名义总利息估算", f"¥ {result_df['应付利息'].sum():,.2f}")
    col_m2.metric("累计本息流出", f"¥ {(result_df['应付本金'].sum() + result_df['应付利息'].sum()):,.2f}")
    col_m3.metric("计息天数合计", f"{result_df['计息天数'].sum()} 天")
    
    st.markdown("### 📊 宏观现金流台账")
    st.dataframe(result_df.style.format(na_rep="-", formatter={
        "期初本金": "{:,.2f}", "应付利息": lambda x: f"{x:,.2f}" if x > 0 else "-", "应付本金": lambda x: f"{x:,.2f}" if x > 0 else "-", "本息合计": "{:,.2f}", "期末剩余本金": "{:,.2f}"
    }).map(lambda x: 'color: orange;' if '⚠️' in str(x) else '', subset=['实际收付日']), height=350, use_container_width=True)
    
    st.markdown("### 🔍 穿透式计息白盒化明细")
    with st.expander("展开查看每个结息周期的 LPR 变动与分段计息过程", expanded=True):
        st.dataframe(details_df.style.format(formatter={"本金余额": "{:,.2f}", "切片利息": "{:,.2f}"}), height=300, use_container_width=True)
        
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
        result_df.to_excel(writer, sheet_name='综合台账', index=False)
        details_df.to_excel(writer, sheet_name='计息明细', index=False)
    
    st.download_button("📥 导出完整台账 (Excel双表单)", data=buffer.getvalue(), file_name=f"信贷台账_{borrower_input}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")
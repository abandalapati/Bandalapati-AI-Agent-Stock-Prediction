import numpy as np
import os
import pandas as pd
import backtrader as bt
import logging
from datetime import datetime
from src.Data_Retrieval.data_fetcher import DataFetcher
from src.Indicators.griffiths_predictor import GriffithsPredictor


logging.basicConfig(level=logging.INFO,
                     format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


#################################
# GRIFFITHS DEFAULTS (global)
#################################
GRIFFITHS_DEFAULTS = {
    'make_stationary' : False,
    'use_log_diff' : False,
    'length': 18,
    'lower_bound': 18,
    'upper_bound': 40,
    'bars_fwd': 2,
    'peak_decay': 0.991,
    'initial_peak': 0.0001,
    'scale_to_price': False,
    'allocation' : 1.0
}

def dict_to_params(d: dict) -> tuple:
    """
    Convert a dict into a Backtrader 'params' tuple,
    i.e. { 'length': 18 } -> (('length', 18), ...)
    """
    return tuple((k, v) for k, v in d.items())

#####################################
# Indicator wrapped for BT
#####################################
class GriffithsPredictorBT(bt.Indicator):
    """
    Wraps the existing indicator into a Backtrader Indicator.
    """
    lines = ('gp_signal',)
    params = dict_to_params(GRIFFITHS_DEFAULTS)

    def __init__(self):
        self.addminperiod(self.p.upper_bound)  # Ensure enough data is available

        size = len(self.data)  # Get data size
        predictions = np.zeros(size)

        close_prices = np.array(self.data.close)  # Convert to NumPy array

        # Instantiate predictor
        gp = GriffithsPredictor(
            close_prices=close_prices, 
            make_stationary=self.p.make_stationary,
            use_log_diff=self.p.use_log_diff,
            length=self.p.length,
            lower_bound=self.p.lower_bound,
            upper_bound=self.p.upper_bound,
            bars_fwd=self.p.bars_fwd,
            peak_decay=self.p.peak_decay,
            initial_peak=self.p.initial_peak,
            scale_to_price=self.p.scale_to_price
        )

        # Get the predictions
        self.preds, _ = gp.predict_price()

    def once(self, start, end):
        """
        'once' is called when loading the full dataset in backtesting mode.
        """
        for i in range(self.data.buflen()):
            self.lines.gp_signal[i] = self.preds[i]

#######################################
# Strategy
#######################################
class ReversedGriffithsCrossStrategy(bt.Strategy):
    params = dict_to_params(GRIFFITHS_DEFAULTS) + (
        ('stop_loss_pct', 0.05),       # 5% stop loss
        ('max_risk_pct', 0.02),        # Risk 2% of portfolio per trade
        ('trail_stop_pct', 0.10),      # 10% trailing stop
    )

    def __init__(self):
        # Add our indicator to the data
        self.gp_ind = GriffithsPredictorBT(
            self.data,
            length=self.p.length,
            lower_bound=self.p.lower_bound,
            upper_bound=self.p.upper_bound,
            bars_fwd=self.p.bars_fwd,
            peak_decay=self.p.peak_decay,
            initial_peak=self.p.initial_peak,
            scale_to_price=self.p.scale_to_price
        )

        self.gp_signal = self.gp_ind.gp_signal
        self.order = None
        self.pending_entry = None
        self.stop_order = None
        self.entry_price = None
        self.entry_date = None

    def bearish_cross(self, prev_bar, current_bar):
        # REVERSED: Buy when crossing below zero
        return prev_bar >= 0 and current_bar < 0

    def bullish_cross(self, prev_bar, current_bar):
        # REVERSED: Sell when crossing above zero
        return prev_bar <= 0 and current_bar > 0

    def log_position(self):
        pos_size = self.position.size if self.position else 0
        pos_type = 'NONE'
        if pos_size > 0:
            pos_type = 'LONG'
        elif pos_size < 0:
            pos_type = 'SHORT'
        logging.info(f"{self.data.datetime.date(0)}: POSITION UPDATE: {pos_type} {pos_size} shares")

    def notify_order(self, order):
        date = self.data.datetime.date(0)
        if order.status in [order.Completed]:
            if order.isbuy() and not self.position.size < 0:
                logging.info(f"{date}: BUY EXECUTED, Price: {order.executed.price:.2f}, Size: {order.executed.size}")
                self.entry_date = date
            elif order.issell() and not self.position.size > 0:
                logging.info(f"{date}: SELL EXECUTED, Price: {order.executed.price:.2f}, Size: {order.executed.size}")
                self.entry_date = date

            self.log_position()

            # Enter pending position after close executes
            if self.pending_entry:
                cash = self.broker.getcash()
                price = self.data.close[0]
                size = int((cash / price) * 0.95)
                if size > 0:
                    if self.pending_entry == 'LONG':
                        self.order = self.buy(size=size)
                        logging.info(f"{date}: BUY {size} shares at {price:.2f}")
                    elif self.pending_entry == 'SHORT':
                        self.order = self.sell(size=size)
                        logging.info(f"{date}: SELL {size} shares at {price:.2f}")
                self.pending_entry = None

            self.log_position()

        elif order.status in [order.Margin, order.Rejected]:
            logging.warning(f"{self.data.datetime.date(0)}: Order Failed - Margin/Rejected")
            self.order = None
            self.pending_entry = None

        if order.status in [order.Completed, order.Canceled, order.Margin, order.Rejected]:
            self.order = None

    def next(self):
        date = self.data.datetime.date(0)
        
        # Skip if there's a pending order
        if self.order:
            return  
        
        # Get current signal values
        gp_val = self.gp_signal[0]
        gp_prev = self.gp_signal[-1] if len(self.gp_signal) > 1 else 0
        
        # Skip if signal is too weak (filter out noise)
        if abs(gp_val) < 0.005:
            return
            
        # Calculate current portfolio value and price
        portfolio_value = self.broker.getvalue()
        current_price = self.data.close[0]
        
        # Calculate position size based on risk management (2% max risk per trade)
        max_risk_amount = portfolio_value * 0.02
        stop_loss_pct = 0.05  # 5% stop loss
        
        # Calculate proper position size
        risk_per_share = current_price * stop_loss_pct
        proper_size = max(1, int(max_risk_amount / risk_per_share))
        
        # Cap position size to a maximum of 15% of portfolio value
        max_size = int((portfolio_value * 0.15) / current_price)
        proper_size = min(proper_size, max_size)
        
        # Check for signal crossovers
        if self.bearish_cross(gp_prev, gp_val):  # REVERSED: Buy when crossing below zero
            # Only buy if volume and momentum conditions are favorable
            if self.data.volume[0] > self.data.volume[-5]:  # Higher than 5-day average volume
                if self.position:
                    if self.position.size < 0:  # Short position active
                        logging.info(f"{date}: CLOSING SHORT POSITION BEFORE GOING LONG")
                        # Cancel any existing stop orders
                        if self.stop_order:
                            self.cancel(self.stop_order)
                            self.stop_order = None
                        
                        # Close position and prepare for new entry
                        self.order = self.close()
                        self.pending_entry = 'LONG'
                else:
                    # Calculate stop loss price
                    stop_price = current_price * (1 - stop_loss_pct)
                    
                    # Enter long position with proper size
                    self.order = self.buy(size=proper_size)
                    logging.info(f"{date}: BUY {proper_size} shares at {current_price:.2f} with stop at {stop_price:.2f}")
                    
                    # Set stop loss order
                    self.stop_order = self.sell(size=proper_size, 
                                            exectype=bt.Order.Stop,
                                            price=stop_price)
                    
                    # Store entry price for trailing stop calculations
                    self.entry_price = current_price
        
        elif self.bullish_cross(gp_prev, gp_val):  # REVERSED: Sell when crossing above zero
            # Only sell if volume and momentum conditions are favorable
            if self.data.volume[0] > self.data.volume[-5]:  # Higher than 5-day average volume
                if self.position:
                    if self.position.size > 0:  # Long position active
                        logging.info(f"{date}: CLOSING LONG POSITION BEFORE GOING SHORT")
                        # Cancel any existing stop orders
                        if self.stop_order:
                            self.cancel(self.stop_order)
                            self.stop_order = None
                        
                        # Close position and prepare for new entry
                        self.order = self.close()
                        self.pending_entry = 'SHORT'
                else:
                    # Calculate stop loss price (for short position)
                    stop_price = current_price * (1 + stop_loss_pct)
                    
                    # Enter short position with proper size
                    self.order = self.sell(size=proper_size)
                    logging.info(f"{date}: SELL {proper_size} shares at {current_price:.2f} with stop at {stop_price:.2f}")
                    
                    # Set stop loss order
                    self.stop_order = self.buy(size=proper_size, 
                                            exectype=bt.Order.Stop,
                                            price=stop_price)
                    
                    # Store entry price for trailing stop calculations
                    self.entry_price = current_price
        
        # Update trailing stops for existing positions
        elif self.position and not self.pending_entry:
            # For long positions
            if self.position.size > 0 and self.entry_price and self.stop_order:
                # Calculate trailing stop level (10% trailing)
                trail_pct = 0.10
                new_stop = current_price * (1 - trail_pct)
                
                # Get the current stop price
                current_stop = self.stop_order.created.price
                
                # Only move stop up, never down (for long positions)
                if new_stop > current_stop:
                    # Cancel existing stop
                    self.cancel(self.stop_order)
                    
                    # Create new stop at higher level
                    self.stop_order = self.sell(size=self.position.size, 
                                            exectype=bt.Order.Stop,
                                            price=new_stop)
                    logging.info(f"{date}: UPDATED STOP for LONG position to {new_stop:.2f}")
            
            # For short positions
            elif self.position.size < 0 and self.entry_price and self.stop_order:
                # Calculate trailing stop level (10% trailing)
                trail_pct = 0.10
                new_stop = current_price * (1 + trail_pct)
                
                # Get the current stop price
                current_stop = self.stop_order.created.price
                
                # Only move stop down, never up (for short positions)
                if new_stop < current_stop:
                    # Cancel existing stop
                    self.cancel(self.stop_order)
                    
                    # Create new stop at lower level
                    self.stop_order = self.buy(size=abs(self.position.size), 
                                            exectype=bt.Order.Stop,
                                            price=new_stop)
                    logging.info(f"{date}: UPDATED STOP for SHORT position to {new_stop:.2f}")
        
        # Implement time-based exit (exit positions after holding for 10 days)
        if self.position and hasattr(self, 'entry_date'):
            days_held = (date - self.entry_date).days
            if days_held > 10:
                logging.info(f"{date}: CLOSING POSITION after holding for {days_held} days")
                # Cancel any existing stop orders
                if self.stop_order:
                    self.cancel(self.stop_order)
                    self.stop_order = None
                
                # Close position
                self.order = self.close()
                self.entry_date = None


def run_backtest(strategy_class, data_feed, cash=10000, commission=0.001, **kwargs):
    cerebro = bt.Cerebro(runonce=True, preload=True)
    
    # Add strategy with any additional parameters
    strategy_params = dict(GRIFFITHS_DEFAULTS)
    strategy_params.update(kwargs)
    cerebro.addstrategy(strategy_class, **strategy_params)
    
    cerebro.adddata(data_feed)
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission)

    # Enhanced analyzers
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', riskfreerate=0.01)
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade')
    cerebro.addanalyzer(bt.analyzers.SQN, _name='sqn')
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='monthly_returns', timeframe=bt.TimeFrame.Months)

    # Run the backtest
    logging.info(f"Running {strategy_class.__name__} Strategy...")
    result = cerebro.run()

    # Extract the strategy and analyzer data
    strat = result[0]
    
    # Get all analysis results
    sharpe = strat.analyzers.sharpe.get_analysis()
    returns = strat.analyzers.returns.get_analysis()
    drawdown = strat.analyzers.drawdown.get_analysis()
    trades = strat.analyzers.trade.get_analysis() if hasattr(strat.analyzers, 'trade') else {}
    sqn = strat.analyzers.sqn.get_analysis() if hasattr(strat.analyzers, 'sqn') else {}
    monthly = strat.analyzers.monthly_returns.get_analysis() if hasattr(strat.analyzers, 'monthly_returns') else {}
    
    # Print summary results
    print("\n" + "="*50)
    print(f"STRATEGY PERFORMANCE: {strategy_class.__name__}")
    print("="*50)
    
    # Returns
    print("\nRETURNS:")
    print(f"  Total Return: {returns.get('rtot', 0)*100:.2f}%")
    print(f"  Average Daily Return: {returns.get('ravg', 0)*100:.4f}%")
    print(f"  Annualized Return: {((1+returns.get('ravg', 0))**252 - 1)*100:.2f}%")
    
    # Drawdown - with sanity checks to fix extreme values
    dd_pct = drawdown.get('drawdown', 0) * 100
    if dd_pct > 100:
        print(f"  WARNING: Calculated drawdown exceeds 100% ({dd_pct:.2f}%), likely a calculation error")
        dd_pct = min(dd_pct, 100.0)
    print(f"  Maximum Drawdown: {dd_pct:.2f}%")
    print(f"  Max Drawdown Duration: {drawdown.get('maxdrawdownperiod', 'N/A')}")
    
    # Sharpe
    sharpe_ratio = sharpe.get('sharperatio', None)
    if sharpe_ratio is not None:
        print(f"  Sharpe Ratio: {sharpe_ratio:.2f}")
    else:
        print("  Sharpe Ratio: N/A (insufficient data or all negative returns)")
    
    # SQN (System Quality Number)
    if sqn:
        print(f"  System Quality Number (SQN): {sqn.get('sqn', 0):.2f}")
    
    # Trade Analysis
    if trades:
        print("\nTRADE ANALYSIS:")
        total_trades = trades.get('total', {}).get('total', 0)
        won = trades.get('won', {}).get('total', 0)
        lost = trades.get('lost', {}).get('total', 0)
        win_rate = won / total_trades * 100 if total_trades > 0 else 0
        
        print(f"  Total Trades: {total_trades}")
        print(f"  Win Rate: {win_rate:.2f}%")
        
        if hasattr(trades.get('won', {}), 'pnl'):
            avg_win = trades.get('won', {}).get('pnl', {}).get('average', 0)
            print(f"  Average Win: ${avg_win:.2f}")
        
        if hasattr(trades.get('lost', {}), 'pnl'):
            avg_loss = trades.get('lost', {}).get('pnl', {}).get('average', 0)
            print(f"  Average Loss: ${avg_loss:.2f}")
        
        # Profit Factor
        gross_profit = trades.get('won', {}).get('pnl', {}).get('total', 0)
        gross_loss = trades.get('lost', {}).get('pnl', {}).get('total', 0)
        profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else 0
        print(f"  Profit Factor: {profit_factor:.2f}")
        
        # Average trade duration
        if hasattr(trades, 'len'):
            avg_trade_bars = trades.get('len', {}).get('average', 0)
            print(f"  Average Trade Duration: {avg_trade_bars:.1f} bars")
    
    # Monthly returns heatmap (simplified text version)
    if monthly:
        years = set(date.year for date in monthly.keys())
        if years:
            print("\nMONTHLY RETURNS:")
            for year in sorted(years):
                year_returns = []
                for month in range(1, 13):
                    for date, ret in monthly.items():
                        if date.year == year and date.month == month:
                            year_returns.append(f"{ret*100:6.2f}%")
                            break
                    else:
                        year_returns.append("   N/A ")
                
                print(f"  {year}: {' | '.join(year_returns)}")
    
    # Plot results if requested
    if kwargs.get('plot', False):
        cerebro.plot()
    
    return result


if __name__ == '__main__':
    cash = 10000
    commission=0.001

    symbol = 'SPY'
    start = datetime(2020,1,1)
    end = datetime.today()

    # Load the data from the Excel file
    script_dir = os.path.dirname(os.path.abspath(__file__))  # Get script's directory
    data_file = os.path.join(script_dir, f"{symbol}_data.xlsx")

    print(f"Loading data from: {data_file}")

    data = pd.read_excel(data_file, index_col='Date', parse_dates=True)

    # Convert pandas DataFrame into Backtrader data feed
    data_feed = bt.feeds.PandasData(
        dataname=data,
        fromdate=start,
        todate=end,
        timeframe=bt.TimeFrame.Minutes  # Set to minute data
    )

    
    print("\n*********************************************")
    print("********* REVERSED Griffiths CROSS **********")
    print("*********************************************")
    run_backtest(strategy_class=ReversedGriffithsCrossStrategy, data_feed=data_feed, cash=cash, commission=commission)


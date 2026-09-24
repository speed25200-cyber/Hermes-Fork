import sys, os, pickle, time
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/calbasis')
import cb_backtest as cb
HERE = os.path.dirname(os.path.abspath(__file__))
def load():
    fn = os.path.join(HERE, 'assets.pkl')
    if os.path.exists(fn):
        return pickle.load(open(fn, 'rb'))
    t = time.time()
    AS = {a: cb.load_asset(a) for a in ['BTC', 'ETH']}
    pickle.dump(AS, open(fn, 'wb'))
    print('built assets in', round(time.time() - t), 's')
    return AS

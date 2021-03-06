import React from 'react';
import {action} from '@storybook/addon-actions';
import {withInfo} from '@storybook/addon-info';
import {text} from '@storybook/addon-knobs';

import ShareIssue from 'app/views/organizationGroupDetails/actions/shareIssue';

export default {
  title: 'Features/Issues/Share Issue',
};

class ShareSimulator extends React.Component {
  state = {isShared: false, loading: false};
  toggleAction = action('Toggle');

  toggleShare() {
    this.toggleAction();
    this.setState({loading: true});

    // Simulate loading
    setTimeout(() => {
      this.setState(state => ({loading: false, isShared: !state.isShared}));
    }, 1000);
  }

  render() {
    return this.props.children({...this.state, toggleShare: () => this.toggleShare()});
  }
}

export const Default = withInfo('todo')(() => {
  return (
    <ShareSimulator>
      {({isShared, loading, toggleShare}) => (
        <ShareIssue
          loading={loading}
          isShared={isShared}
          shareUrl={text(
            'shareUrl',
            'https://sentry.io/share/issue/thisisanexampleshareurl/'
          )}
          onToggle={toggleShare}
          onReshare={action('Reshare')}
        />
      )}
    </ShareSimulator>
  );
});

Default.story = {
  name: 'default',
};
